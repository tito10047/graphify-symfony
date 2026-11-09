"""All Symfony extractors. Each reads files (never executes project code) and
emits nodes/edges through the shared Emitter."""

from __future__ import annotations

import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import php as phpmod
from .detect import App, Project, container_dumps, join, load_yaml
from .graph import Emitter, GraphIndex
from .ids import file_node_id, global_id, make_id, scoped_id
from .twig import TemplateResolver, TwigFile, parse_twig

SKIP_PHP_SEGMENTS = {"vendor", "tests", "test", "Tests", "migrations", "Migrations", "var", "node_modules"}
SKIP_TWIG_SEGMENTS = {"vendor", "node_modules", "var", ".git", "graphify-out"}

# relation names are chosen from graphify's DEFAULT_AFFECTED_RELATIONS so
# `graphify affected` walks them without --relation; sf_kind says what it is.
REL_EXTENDS = "extends"
REL_EMBEDS = "embeds"
REL_IMPORTS = "imports"
REL_MIXES = "mixes_in"
REL_USES = "uses"
REL_REFS = "references"
REL_DEFINES = "defines"

KNOWN_EVENT_CONSTS = {
    "KernelEvents": {
        "REQUEST": "kernel.request", "EXCEPTION": "kernel.exception", "VIEW": "kernel.view",
        "CONTROLLER": "kernel.controller", "CONTROLLER_ARGUMENTS": "kernel.controller_arguments",
        "RESPONSE": "kernel.response", "TERMINATE": "kernel.terminate", "FINISH_REQUEST": "kernel.finish_request",
    },
    "ConsoleEvents": {
        "COMMAND": "console.command", "TERMINATE": "console.terminate", "ERROR": "console.error",
        "SIGNAL": "console.signal",
    },
    "Events": {},  # Doctrine\ORM\Events::postPersist -> doctrine postPersist (handled below)
}


@dataclass
class Ctx:
    project: Project
    index: GraphIndex
    em: Emitter
    stats: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    php: dict[str, phpmod.PhpFile] = field(default_factory=dict)
    fqcn: dict[str, list[tuple[str, phpmod.PhpClass]]] = field(default_factory=lambda: defaultdict(list))
    templates: dict[str, list[App]] = field(default_factory=dict)  # rel -> apps that see it
    template_ids: dict[str, str] = field(default_factory=dict)
    twig: dict[str, TwigFile] = field(default_factory=dict)
    resolvers: dict[str, TemplateResolver] = field(default_factory=dict)  # app.rel -> resolver
    components: dict[str, dict[str, tuple]] = field(default_factory=dict)  # app.rel -> name -> entry
    component_of_template: dict[str, tuple] = field(default_factory=dict)  # template rel -> entry
    stimulus: dict[str, dict[str, str]] = field(default_factory=dict)  # app.rel -> name -> node id
    ignored: object = None
    boot_container: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    samples: dict[str, list] = field(default_factory=lambda: defaultdict(list))

    def miss(self, key: str, what) -> None:
        self.stats[key] += 1
        if len(self.samples[key]) < 15 and what not in self.samples[key]:
            self.samples[key].append(what)

    # -- helpers ---------------------------------------------------------
    def abs(self, rel: str) -> Path:
        return self.project.root / rel

    def is_ignored(self, rel: str) -> bool:
        if self.ignored is None:
            return False
        try:
            return bool(self.ignored(self.abs(rel)))
        except Exception:
            return False

    def read(self, rel: str) -> str | None:
        try:
            with open(self.abs(rel), encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def php_full(self, rel: str) -> phpmod.PhpFile | None:
        pf = self.php.get(rel)
        if pf is not None and pf.parsed:
            return pf
        text = self.read(rel)
        if text is None:
            return pf
        pf = phpmod.full_scan(rel, text)
        self.php[rel] = pf
        return pf

    def find_class(self, fqcn: str, near: str = "") -> tuple[str, phpmod.PhpClass] | None:
        cands = self.fqcn.get(fqcn.lstrip("\\"))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        app = self.project.app_for(near) if near else None
        if app and app.rel:
            inside = [c for c in cands if c[0].startswith(app.rel + "/")]
            if inside:
                return inside[0]
        outside = [c for c in cands if not any(a.rel and c[0].startswith(a.rel + "/") for a in self.project.apps)]
        return (outside or cands)[0]

    def class_node(self, fqcn: str, near: str = "", allow_external: bool = True) -> str | None:
        if not fqcn:
            return None
        hit = self.find_class(fqcn, near)
        if hit:
            rel, cls = hit
            nid = self.index.class_id(rel, cls.name)
            if nid:
                return nid
        if allow_external:
            return self.index.external_class(fqcn)
        return None

    def method_node(self, rel: str, cls: phpmod.PhpClass, method: str | None) -> str | None:
        cid = self.index.class_id(rel, cls.name)
        if method:
            mid = self.index.method_id(cid, method)
            if mid:
                return mid
        return cid

    def template_node(self, rel: str | None) -> str | None:
        return self.template_ids.get(rel) if rel else None

    def resolve_template_fuzzy(self, name: str, near: str) -> str | None:
        """Fallback for custom Twig loaders (e.g. a package rendering through its
        own FilesystemLoader): the template whose path ends with ``name`` and
        shares the longest directory prefix with the referencing file."""
        name = name.lstrip("/")
        if name.startswith("@") or ".." in name:
            return None
        if not hasattr(self, "_by_base"):
            self._by_base = defaultdict(list)
            for r in self.template_ids:
                self._by_base[r.rsplit("/", 1)[-1]].append(r)
        cands = [r for r in self._by_base.get(name.rsplit("/", 1)[-1], ()) if r == name or r.endswith("/" + name)]
        if not cands:
            return None
        near_parts = near.split("/")[:-1]

        def common(r: str) -> int:
            n = 0
            for a, b in zip(near_parts, r.split("/")[:-1]):
                if a != b:
                    break
                n += 1
            return n

        scored = sorted(((common(r), r) for r in cands), reverse=True)
        best = scored[0][0]
        if best < 2 or (len(scored) > 1 and scored[1][0] == best):
            return None
        return scored[0][1]

    def resolve_template(self, name: str, near: str) -> str | None:
        apps = self.templates.get(near) or ([self.project.app_for(near)] if self.project.app_for(near) else [])
        tried = set()
        for app in list(apps) + self.project.apps:
            if app is None or app.rel in tried:
                continue
            tried.add(app.rel)
            r = self.resolvers[app.rel].resolve(name)
            if r and r in self.template_ids:
                return r
            if len(tried) >= len(apps) and apps:
                # only fall back to other apps for monorepo shared templates
                if not name.startswith("@"):
                    break
        return None


def _segments(rel: str) -> list[str]:
    return rel.split("/")


# ================================================================ PHP index
def build_php_index(ctx: Ctx) -> None:
    t = time.perf_counter()
    files = sorted({str(n.get("source_file")) for n in ctx.index.nodes.values()
                    if str(n.get("source_file", "")).endswith(".php")})
    for rel in files:
        segs = _segments(rel)
        if any(s in SKIP_PHP_SEGMENTS for s in segs[:-1]):
            continue
        text = ctx.read(rel)
        if text is None:
            continue
        if phpmod.INTERESTING.search(text):
            pf = phpmod.full_scan(rel, text)
            ctx.stats["php_files_parsed"] += 1
        else:
            pf = phpmod.quick_scan(rel, text)
        ctx.stats["php_files_indexed"] += 1
        ctx.php[rel] = pf
        for c in pf.classes:
            ctx.fqcn[c.fqcn].append((rel, c))
    ctx.timings["php_index"] = time.perf_counter() - t


def _app_classes(ctx: Ctx):
    for rel, pf in ctx.php.items():
        for c in pf.classes:
            yield rel, pf, c


def _attrs(c, *shorts) -> list[phpmod.Attr]:
    return [a for a in c.attrs if a.short in shorts]


def _str(v) -> str | None:
    return v if isinstance(v, str) else None


def _class_ref(v) -> str | None:
    if isinstance(v, tuple) and v and v[0] == "class":
        return v[1]
    if isinstance(v, str) and re.fullmatch(r"\\?[A-Z][\w\\]+", v):
        return v.lstrip("\\")
    return None


# ================================================================ templates
def _local_bundles(ctx: Ctx) -> list[tuple[str, str]]:
    """(namespace, repo-rel template dir) for bundles whose code is in the repo."""
    out = []
    for rel, pf, c in _app_classes(ctx):
        if not c.name.endswith("Bundle") or c.kind != "class":
            continue
        if not pf.parsed:
            pf = ctx.php_full(rel) or pf
            c = next((x for x in pf.classes if x.name == c.name), c)
        if not c.extends or c.extends.rsplit("\\", 1)[-1] not in ("Bundle", "AbstractBundle"):
            continue
        ns = c.name[:-6]
        d = os.path.dirname(rel)
        for base in (d, os.path.dirname(d)):
            for sub in ("templates", "Resources/views"):
                cand = join(base, sub)
                if (ctx.project.root / cand).is_dir():
                    out.append((ns, cand))
    return out


def collect_templates(ctx: Ctx) -> None:
    t = time.perf_counter()
    bundles = _local_bundles(ctx)
    for app in ctx.project.apps:
        dirs = list(app.template_dirs)
        for ns, d in bundles:
            if (ns, d) not in dirs:
                dirs.append((ns, d))
        app.template_dirs = dirs
    seen_files: set[str] = set()
    for app in ctx.project.apps:
        for _ns, d in app.template_dirs:
            base = ctx.project.root / d
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [x for x in dirnames if x not in SKIP_TWIG_SEGMENTS and not x.startswith(".")]
                for fn in filenames:
                    if not fn.endswith(".twig"):
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, fn), ctx.project.root).replace(os.sep, "/")
                    if rel not in seen_files:
                        seen_files.add(rel)
                    lst = ctx.templates.setdefault(rel, [])
                    if app not in lst:
                        lst.append(app)
    exists = lambda r: r in ctx.templates  # noqa: E731
    for app in ctx.project.apps:
        ctx.resolvers[app.rel] = TemplateResolver(ctx.project.root, app.template_dirs, exists)

    # labels: logical name, made unique with the repo path when needed
    names: dict[str, str] = {}
    for rel, apps in sorted(ctx.templates.items()):
        if ctx.is_ignored(rel):
            ctx.stats["templates_ignored"] += 1
            continue
        own = ctx.project.app_for(rel)
        order = ([own] if own in apps else []) + [a for a in apps if a is not own]
        logical = None
        for a in order:
            logical = ctx.resolvers[a.rel].logical_name(rel)
            if logical:
                break
        names[rel] = logical or rel
    dup = Counter(names.values())
    for rel, logical in names.items():
        label = logical if dup[logical] == 1 else rel
        nid = file_node_id(rel)
        if nid in ctx.index.ids:
            nid = make_id(nid, "twig")
        ctx.template_ids[rel] = ctx.em.node(nid, label, sf_type="template", source_file=rel, line=1,
                                            file_type="code", sf_twig_name=logical)
        ctx.stats["nodes.template"] += 1
    ctx.timings["templates_collect"] = time.perf_counter() - t


TWIG_KIND = {
    "extends": (REL_EXTENDS, "extends"),
    "include": (REL_EMBEDS, "include"),
    "embed": (REL_EMBEDS, "embed"),
    "import": (REL_IMPORTS, "import"),
    "use": (REL_MIXES, "use"),
    "form_theme": (REL_USES, "form_theme"),
    "source": (REL_USES, "source"),
    "block_ref": (REL_USES, "block_ref"),
}


def extract_templates(ctx: Ctx) -> None:
    t = time.perf_counter()
    blocks: dict[str, dict[str, str]] = {}
    parents: dict[str, list[str]] = defaultdict(list)
    for rel, tid in ctx.template_ids.items():
        text = ctx.read(rel)
        if text is None:
            continue
        tf = parse_twig(text)
        ctx.twig[rel] = tf
        ctx.stats["twig_dynamic_refs_skipped"] += tf.dynamic_refs
        bmap = {}
        for name, line in tf.blocks:
            if name in bmap:
                continue
            bid = scoped_id(rel, "block", name)
            bmap[name] = ctx.em.node(bid, f"block {name} ({ctx.em.nodes[tid]['label']})", sf_type="twig_block",
                                     source_file=rel, line=line, file_type="code", sf_block=name)
            ctx.em.edge(tid, bid, REL_DEFINES, kind="defines_block", source_file=rel, line=line)
            ctx.stats["nodes.twig_block"] += 1
        blocks[rel] = bmap
    for rel, tf in ctx.twig.items():
        tid = ctx.template_ids[rel]
        for ref in tf.refs:
            relation, kind = TWIG_KIND[ref.kind]
            for name in ref.names:
                conf = "INFERRED" if ref.dynamic else "EXTRACTED"
                target = ctx.resolve_template(name, rel)
                if not target:
                    target = ctx.resolve_template_fuzzy(name, rel)
                    if target:
                        conf = "INFERRED"
                        ctx.stats["twig_resolved_by_path_suffix"] += 1
                if not target:
                    ctx.miss("twig_unresolved", f"{rel}: {name}")
                    continue
                if ref.kind == "extends":
                    parents[rel].append(target)
                ok = ctx.em.edge(tid, ctx.template_ids[target], relation, kind=kind, confidence=conf,
                                 source_file=rel, line=ref.line, sf_twig_name=name)
                if ok:
                    ctx.stats[f"edges.twig_{kind}"] += 1
                if ref.kind == "block_ref" and ref.block and ref.block in blocks.get(target, {}):
                    ctx.em.edge(tid, blocks[target][ref.block], REL_USES, kind="block_ref",
                                source_file=rel, line=ref.line)
    # block overrides along the extends chain: child.block -> nearest ancestor's block
    for rel, plist in parents.items():
        for bname, bid in blocks.get(rel, {}).items():
            stack, seen = list(plist), set()
            while stack:
                p = stack.pop(0)
                if p in seen:
                    continue
                seen.add(p)
                pb = blocks.get(p, {}).get(bname)
                if pb:
                    if ctx.em.edge(bid, pb, REL_EXTENDS, kind="overrides_block", source_file=rel,
                                   line=int(ctx.em.nodes[bid]["source_location"][1:])):
                        ctx.stats["edges.twig_overrides_block"] += 1
                    break
                stack.extend(parents.get(p, []))
    # app-wide form themes from twig.yaml
    for app in ctx.project.apps:
        cfg = join(app.rel, "config/packages/twig.yaml")
        cfg_file = ctx.index.file_id(cfg)
        for name in app.form_themes:
            target = ctx.resolvers[app.rel].resolve(name)
            if cfg_file and target in ctx.template_ids:
                if ctx.em.edge(cfg_file, ctx.template_ids[target], REL_USES, kind="form_theme",
                               source_file=cfg, line=1):
                    ctx.stats["edges.twig_form_theme"] += 1
    ctx.timings["templates_parse"] = time.perf_counter() - t


# ================================================================ PHP -> Twig
def extract_php_templates(ctx: Ctx) -> None:
    for rel, pf in ctx.php.items():
        if not pf.twig_refs:
            continue
        for line, name, kind, extra in pf.twig_refs:
            if kind == "component_template":
                continue  # handled by components
            target = ctx.resolve_template(name, rel)
            fuzzy = False
            if not target:
                target = ctx.resolve_template_fuzzy(name, rel)
                fuzzy = bool(target)
                if fuzzy:
                    ctx.stats["php_template_resolved_by_path_suffix"] += 1
            if not target:
                ctx.miss("php_template_unresolved", f"{rel}:{line}: {name}")
                continue
            src = ctx.index.symbol_at(rel, line)
            if not src:
                continue
            conf = "INFERRED" if kind == "template_ref" or fuzzy else "EXTRACTED"
            sf_kind = "render" if kind in ("render", "template_attr") else "template_ref"
            if ctx.em.edge(src, ctx.template_ids[target], REL_USES, kind=sf_kind, confidence=conf,
                           source_file=rel, line=line, sf_call=extra):
                ctx.stats[f"edges.php_{sf_kind}"] += 1
            if extra and ":" in extra:
                bname = extra.split(":", 1)[1]
                bid = scoped_id(target, "block", bname)
                ctx.em.edge(src, bid, REL_USES, kind="render_block", source_file=rel, line=line)


# ================================================================ components
COMPONENT_ATTRS = ("AsTwigComponent", "AsLiveComponent", "AsSdcComponent")


def _component_defaults(app: App, fqcn: str) -> tuple[str, dict] | None:
    best = None
    for ns, cfg in app.component_defaults.items():
        if fqcn.startswith(ns) and (best is None or len(ns) > len(best[0])):
            best = (ns, cfg)
    return best


def extract_components(ctx: Ctx) -> None:
    per_app: dict[str, dict[str, tuple]] = {a.rel: {} for a in ctx.project.apps}
    for rel, pf, c in list(_app_classes(ctx)):
        attrs = _attrs(c, *COMPONENT_ATTRS)
        if not attrs:
            continue
        a = attrs[0]
        app = ctx.project.app_for(rel)
        apps = [app] if app else ctx.project.apps
        explicit_name = _str(a.kwargs.get("name")) or (_str(a.args[0]) if a.args else None)
        explicit_tpl = _str(a.kwargs.get("template")) or (_str(a.args[1]) if len(a.args) > 1 else None)
        for ap in apps:
            if ap is None:
                continue
            name = explicit_name
            cands: list[tuple[str, str]] = []  # (template name or rel path marker, confidence)
            sdc = ap.sdc_namespace and c.fqcn.startswith(ap.sdc_namespace + "\\")
            if explicit_tpl:
                cands.append((explicit_tpl, "EXTRACTED"))
            if sdc:
                if not name:
                    name = c.fqcn[len(ap.sdc_namespace) + 1:].replace("\\", ":")
                cands.append(("rel:" + join(os.path.dirname(rel), c.name + ".html.twig"), "INFERRED"))
            d = _component_defaults(ap, c.fqcn)
            if d:
                ns, cfg = d
                sub = c.fqcn[len(ns):]
                if not name:
                    name = cfg.get("name_prefix", "") + sub.replace("\\", ":")
                tdir = cfg.get("template_directory", "components/").rstrip("/")
                cands.append((f"{tdir}/{sub.replace(chr(92), '/')}.html.twig", "INFERRED"))
                if explicit_name:
                    cands.append((f"{tdir}/{explicit_name.replace(':', '/')}.html.twig", "INFERRED"))
            if not name:
                name = c.name
            cands.append((f"components/{name.replace(':', '/')}.html.twig", "INFERRED"))
            tpl, conf = None, None
            for cand, cf in cands:
                if cand.startswith("rel:"):
                    r = cand[4:]
                    r = r if r in ctx.template_ids else None
                else:
                    r = ctx.resolve_template(cand, rel) if ap is None else ctx.resolvers[ap.rel].resolve(cand)
                    r = r if r in ctx.template_ids else None
                if r:
                    tpl, conf = r, cf
                    break
            entry = ("class", c.fqcn, rel, c, tpl, a.short)
            per_app[ap.rel][name] = entry
            cid = ctx.index.class_id(rel, c.name)
            if tpl and cid:
                ctx.component_of_template[tpl] = entry
                if ctx.em.edge(cid, ctx.template_ids[tpl], REL_USES, kind="component_template", confidence=conf,
                               source_file=rel, line=c.line, sf_component=name):
                    ctx.stats["edges.component_template"] += 1
            # co-located Stimulus controller (ux-sdc)
            if sdc and cid:
                for ext in (".js", ".ts"):
                    js = join(os.path.dirname(rel), c.name + "_controller" + ext)
                    jid = ctx.index.file_id(js)
                    if jid and ctx.em.edge(cid, jid, REL_USES, kind="sdc_controller", confidence="INFERRED",
                                           source_file=rel, line=c.line):
                        ctx.stats["edges.sdc_controller"] += 1
    # anonymous components
    for ap in ctx.project.apps:
        reg = per_app[ap.rel]
        claimed = {e[4] for e in reg.values() if e[0] == "class" and e[4]}
        anon_roots: list[str] = []
        for ns, d in ap.template_dirs:
            if ns is None:
                anon_roots.append(join(d, ap.anonymous_dir))
        if ap.sdc_dir:
            anon_roots.append(ap.sdc_dir)
        for rel in ctx.template_ids:
            if rel in claimed or not rel.endswith(".html.twig"):
                continue
            for root in anon_roots:
                if rel.startswith(root + "/"):
                    sub = rel[len(root) + 1:-len(".html.twig")]
                    name = sub.replace("/", ":")
                    reg.setdefault(name, ("anon", None, rel, None, rel, "anonymous"))
                    if name.endswith(":index"):
                        reg.setdefault(name[:-6], ("anon", None, rel, None, rel, "anonymous"))
                    break
        ctx.components[ap.rel] = reg
        ctx.stats["components_registered"] += len(reg)
    # usages
    for rel, tf in ctx.twig.items():
        tid = ctx.template_ids[rel]
        app = ctx.project.app_for(rel)
        apps = ctx.templates.get(rel) or ([app] if app else [])
        for name, line, syntax in tf.components:
            entry = None
            for ap in ([app] if app in apps else []) + [x for x in apps if x is not app]:
                entry = ctx.components.get(ap.rel, {}).get(name)
                if entry:
                    break
            if not entry:
                if not name.startswith("ux:"):  # UX Icons/Map etc. live in vendor
                    ctx.miss("component_usage_unresolved", name)
                continue
            if entry[0] == "class":
                target = ctx.index.class_id(entry[2], entry[3].name)
            else:
                target = ctx.template_ids.get(entry[4])
            if ctx.em.edge(tid, target, REL_USES, kind="component", source_file=rel, line=line,
                           sf_component=name, sf_syntax=syntax):
                ctx.stats["edges.component_usage"] += 1


# ================================================================ stimulus
def _stimulus_index(ctx: Ctx) -> None:
    by_app: dict[str, dict[str, str]] = {a.rel: {} for a in ctx.project.apps}
    js_files = [sf for sf in ctx.index.files if re.search(r"_controller\.(?:js|ts|mjs)$", sf) or
                ("/controllers/" in sf and sf.endswith((".js", ".ts")))]
    for sf in js_files:
        fid = ctx.index.file_id(sf)
        if not fid:
            continue
        for ap in ctx.project.apps:
            roots = [join(ap.rel, "assets/controllers")]
            if ap.sdc_dir:
                roots.append(ap.sdc_dir)
            for root in roots:
                if sf.startswith(root + "/"):
                    sub = re.sub(r"(?:[_-]controller)?\.(?:js|ts|mjs)$", "", sf[len(root) + 1:])
                    name = sub.replace("/", "--").replace("_", "-")
                    by_app[ap.rel].setdefault(name, fid)
                    by_app[ap.rel].setdefault(name.lower(), fid)
    # Stimulus controllers shipped by in-repo packages (package.json "symfony.controllers"),
    # named "<vendor>--<package>--<controller>" by StimulusBundle
    import json as _json
    for _pkg, pdir in ctx.project.packages.items():
        for pj in (ctx.project.root / pdir / "assets" / "package.json", ctx.project.root / pdir / "package.json"):
            if not pj.is_file():
                continue
            try:
                data = _json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                continue
            ctrls = ((data.get("symfony") or {}).get("controllers") or {}) if isinstance(data, dict) else {}
            npm = str(data.get("name") or "").lstrip("@").replace("/", "--")
            for cname, spec in ctrls.items():
                main = spec.get("main") if isinstance(spec, dict) else None
                if not main or not npm:
                    continue
                f = os.path.normpath(os.path.join(os.path.dirname(os.path.relpath(pj, ctx.project.root)), main))
                fid = ctx.index.file_id(f.replace(os.sep, "/"))
                if fid:
                    for idx in by_app.values():
                        idx.setdefault(f"{npm}--{cname}", fid)
    ctx.stimulus = by_app


def extract_stimulus(ctx: Ctx) -> None:
    _stimulus_index(ctx)
    for rel, tf in ctx.twig.items():
        if not tf.stimulus:
            continue
        tid = ctx.template_ids[rel]
        apps = ctx.templates.get(rel) or []
        for name, line in tf.stimulus:
            if name == "{{controller}}":
                entry = ctx.component_of_template.get(rel)
                if not entry:
                    continue
                crel = entry[2]
                for ext in (".js", ".ts"):
                    jid = ctx.index.file_id(join(os.path.dirname(crel), entry[3].name + "_controller" + ext))
                    if jid and ctx.em.edge(tid, jid, REL_USES, kind="stimulus", confidence="INFERRED",
                                           source_file=rel, line=line):
                        ctx.stats["edges.stimulus"] += 1
                continue
            norm = name.replace("/", "--").replace("_", "-")
            fid = None
            for ap in apps:
                idx = ctx.stimulus.get(ap.rel, {})
                fid = idx.get(norm) or idx.get(norm.lower())
                if fid:
                    break
            if not fid:
                ctx.miss("stimulus_unresolved", name)
                continue
            if ctx.em.edge(tid, fid, REL_USES, kind="stimulus", source_file=rel, line=line, sf_controller=name):
                ctx.stats["edges.stimulus"] += 1


# ================================================================ routes
ROUTE_LINE_RE = re.compile(r"^\s*'((?:[^'\\]|\\.)+)'\s*=>\s*\[(.*)\],?\s*$")


def _route_path(v):
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and v:
        return next(iter(v.values())) if isinstance(next(iter(v.values())), str) else None
    return None


def _methods(v) -> list[str]:
    if isinstance(v, str):
        return [x.strip().upper() for x in v.split("|") if x.strip()]
    if isinstance(v, list):
        return [str(x).upper() for x in v if isinstance(x, str)]
    return []


def _route_attr(a: phpmod.Attr) -> dict:
    path = _route_path(a.kwargs.get("path")) or (_route_path(a.args[0]) if a.args else None)
    return {
        "path": path,
        "name": _str(a.kwargs.get("name")) or (_str(a.args[1]) if len(a.args) > 1 else None),
        "methods": _methods(a.kwargs.get("methods")),
    }


def _route_label(name: str | None, methods: list[str], path: str | None) -> str:
    parts = ["route", name or "(unnamed)"]
    if methods:
        parts.append("|".join(methods))
    if path:
        parts.append(path)
    return " ".join(parts)


def _emit_route(ctx, rel, cls, method, name, path, methods, line, conf="EXTRACTED", source_file=None, extra=None):
    src_file = source_file or rel
    key = name or f"{path}_{'_'.join(methods)}_{method}"
    rid = scoped_id(src_file, "route", key)
    if rid in ctx.em.nodes:
        return rid
    ctx.em.node(rid, _route_label(name, methods, path), sf_type="route", source_file=src_file, line=line,
                file_type="concept", sf_route_name=name, sf_path=path, sf_methods=methods or None, **(extra or {}))
    target = ctx.method_node(rel, cls, method) if cls is not None else None
    if ctx.em.edge(rid, target, REL_REFS, kind="route", confidence=conf, source_file=src_file, line=line):
        ctx.stats["nodes.route"] += 1
        ctx.stats["edges.route"] += 1
    return rid


def _parse_route_cache(path: Path) -> dict[str, tuple[str | None, str]]:
    """url_generating_routes.php -> {name: (controller, path)} (no PHP execution)."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = ROUTE_LINE_RE.match(line)
        if not m:
            continue
        name, body = m.group(1), m.group(2)
        cm = re.search(r"'_controller'\s*=>\s*'((?:[^'\\]|\\.)+)'", body)
        ctrl = cm.group(1).replace("\\\\", "\\") if cm else None
        # tokens: list of ['text', '/x'] / ['variable', '/', 'req', 'name', ...] in reverse order
        tm = re.search(r"\]\s*,\s*\[(\[(?:'text'|'variable').*?\])\]\s*,\s*\[", body)
        pth = ""
        if tm:
            toks = re.findall(r"\[\s*'(text|variable)'\s*,\s*'((?:[^'\\]|\\.)*)'(?:\s*,\s*'(?:[^'\\]|\\.)*'\s*,\s*'((?:[^'\\]|\\.)*)')?", tm.group(1))
            for kind, a, var in reversed(toks):
                pth += a if kind == "text" else f"{a}{{{var}}}"
        out[name] = (ctrl, pth or "/")
    return out


def extract_routes(ctx: Ctx) -> None:
    attr_names: dict[str, str] = {}  # route name -> node id
    for rel, pf, c in list(_app_classes(ctx)):
        if not pf.parsed:
            continue
        croute = next((a for a in c.attrs if a.short == "Route"), None)
        prefix = _route_attr(croute) if croute else {"path": "", "name": "", "methods": []}
        any_method = False
        for m in c.methods:
            for a in m.attrs:
                if a.short != "Route":
                    continue
                any_method = True
                r = _route_attr(a)
                path = (prefix.get("path") or "") + (r["path"] or "")
                name = ((prefix.get("name") or "") + r["name"]) if r["name"] else None
                methods = r["methods"] or prefix.get("methods") or []
                rid = _emit_route(ctx, rel, c, m.name, name, path or "/", methods, a.line)
                if name:
                    attr_names[name] = rid
        if croute and not any_method and c.method("__invoke"):
            r = prefix
            rid = _emit_route(ctx, rel, c, "__invoke", r["name"] or None, r["path"] or "/", r["methods"], croute.line)
            if r["name"]:
                attr_names[r["name"]] = rid
    # compiled route cache: full paths + routes from YAML/loaders
    for app in ctx.project.apps:
        cache = app.root / "var" / "cache" / "dev" / "url_generating_routes.php"
        if not cache.is_file():
            continue
        routes = _parse_route_cache(cache)
        ctx.stats["route_cache_entries"] += len(routes)
        for name, (ctrl, pth) in routes.items():
            if "::" in name or "\\" in name:
                continue  # FQCN::method aliases Symfony adds next to the real name
            if name in attr_names:
                node = ctx.em.nodes.get(attr_names[name])
                if node is not None and node.get("sf_path") != pth:
                    node["sf_full_path"] = pth
                    node["label"] = _route_label(name, node.get("sf_methods") or [], pth)
                    node["norm_label"] = node["label"].lower()
                continue
            if not ctrl or "::" not in ctrl or name.startswith("_"):
                continue
            cls_name, meth = ctrl.split("::", 1)
            hit = ctx.find_class(cls_name, app.rel + "/" if app.rel else "")
            if not hit:
                ctx.miss("route_cache_vendor_skipped", f"{name} -> {ctrl}")
                continue
            crel, cls = hit
            rid = _emit_route(ctx, crel, cls, meth, name, pth, [], cls.line, source_file=crel,
                              extra={"sf_from": "route_cache"})
            attr_names[name] = rid
    # simple YAML routes
    for app in ctx.project.apps:
        files = [app.root / "config" / "routes.yaml"] + sorted((app.root / "config" / "routes").glob("*.yaml"))
        for f in files:
            data = load_yaml(f) if f.is_file() else None
            if not isinstance(data, dict):
                continue
            frel = os.path.relpath(f, ctx.project.root).replace(os.sep, "/")
            for name, spec in data.items():
                if not isinstance(spec, dict) or "path" not in spec or name in attr_names:
                    continue
                ctrl = spec.get("controller") or (spec.get("defaults") or {}).get("_controller")
                if not isinstance(ctrl, str):
                    continue
                cls_name, _, meth = ctrl.partition("::")
                hit = ctx.find_class(cls_name, frel)
                if not hit:
                    continue
                crel, cls = hit
                _emit_route(ctx, crel, cls, meth or "__invoke", name, _route_path(spec.get("path")),
                            _methods(spec.get("methods")), 1, source_file=frel)


# ================================================================ doctrine
RELATION_ATTRS = ("ManyToOne", "OneToMany", "ManyToMany", "OneToOne", "Embedded")


def _is_doctrine(a: phpmod.Attr) -> bool:
    return "Doctrine" in a.fqcn or a.fqcn.startswith("ORM\\") or "\\ORM\\" in a.fqcn or a.fqcn.startswith("Mapping\\")


def extract_doctrine(ctx: Ctx) -> None:
    for rel, pf, c in list(_app_classes(ctx)):
        if not pf.parsed:
            continue
        cid = ctx.index.class_id(rel, c.name)
        if not cid:
            continue
        for a in c.attrs:
            if a.short == "Entity" and _is_doctrine(a):
                repo = _class_ref(a.kwargs.get("repositoryClass"))
                if repo and not a.annotation and "\\" not in repo:
                    repo = pf.resolve(repo)
                if repo:
                    tgt = ctx.class_node(repo, rel)
                    if ctx.em.edge(cid, tgt, REL_REFS, kind="doctrine_repository", source_file=rel, line=a.line):
                        ctx.stats["edges.doctrine_repository"] += 1
        for p in c.props:
            for a in p.attrs:
                if a.short not in RELATION_ATTRS or not _is_doctrine(a):
                    continue
                target = _class_ref(a.kwargs.get("targetEntity") or a.kwargs.get("class") or (a.args[0] if a.args else None))
                conf = "EXTRACTED"
                if target and "\\" not in target:
                    target = pf.resolve(target)
                if not target and a.short in ("ManyToOne", "OneToOne", "Embedded") and p.type:
                    t = p.type.lstrip("?")
                    if "|" not in t:
                        target = pf.resolve(t)
                        conf = "INFERRED"
                if not target:
                    continue
                tgt = ctx.class_node(target, rel)
                if ctx.em.edge(cid, tgt, REL_REFS, kind="doctrine_" + a.short.lower(), confidence=conf,
                               source_file=rel, line=p.line, sf_property=p.name):
                    ctx.stats["edges.doctrine_relation"] += 1


# ================================================================ events / messenger / commands
def _event_node(ctx: Ctx, event, near: str) -> str | None:
    """Node for an event given as name, FQCN or (const, class, NAME)."""
    name = None
    if isinstance(event, tuple):
        if event[0] == "class":
            n = ctx.class_node(event[1], near)
            if n:
                return n
            name = event[1]
        elif event[0] == "const":
            cls, const = event[1], event[2]
            short = cls.rsplit("\\", 1)[-1]
            name = KNOWN_EVENT_CONSTS.get(short, {}).get(const)
            if not name and short == "Events" and "Doctrine" in cls:
                name = f"doctrine.{const}"
            if not name:
                # app-defined constant: try to read its literal value
                hit = ctx.find_class(cls, near)
                if hit:
                    text = ctx.read(hit[0]) or ""
                    m = re.search(rf"const\s+(?:\w+\s+)?{re.escape(const)}\s*=\s*'([^']+)'", text)
                    if m:
                        name = m.group(1)
                name = name or f"{short}::{const}"
    elif isinstance(event, str) and event:
        if "\\" in event:
            n = ctx.class_node(event, near)
            if n:
                return n
        name = event
    if not name:
        return None
    if "::" in name and "\\" in name:
        cls, const = name.split("::", 1)
        return _event_node(ctx, ("const", cls, const), near)
    nid = global_id("event", name)
    ctx.em.node(nid, f"event {name}", sf_type="event", file_type="concept", sf_event=name)
    return nid


def _camel(event: str) -> str:
    last = re.split(r"[.\\]", event)[-1]
    return "on" + "".join(p[:1].upper() + p[1:] for p in re.split(r"[_\-]", last) if p)


def _first_param_type(pf: phpmod.PhpFile, m: phpmod.Decl | None) -> str | None:
    if m is None or not m.type:
        return None
    t = m.type.lstrip("?")
    if "|" in t or t.lower() in ("object", "mixed", "array", "string", "int"):
        return None
    return pf.resolve(t)


def _command(ctx: Ctx, rel: str, cls: phpmod.PhpClass, name: str, line: int, conf="EXTRACTED") -> None:
    for alias in [x for x in name.split("|") if x][:1]:
        nid = scoped_id(rel, "command", alias)
        ctx.em.node(nid, f"console {alias}", sf_type="console_command", source_file=rel, line=line,
                    file_type="concept", sf_command=alias)
        if ctx.em.edge(nid, ctx.index.class_id(rel, cls.name), REL_REFS, kind="console_command",
                       confidence=conf, source_file=rel, line=line):
            ctx.stats["edges.console_command"] += 1


def _handler(ctx: Ctx, rel, pf, c, method_name, message, line, conf="EXTRACTED") -> None:
    m = c.method(method_name or "__invoke")
    msg = message or _first_param_type(pf, m)
    if not msg:
        return
    src = ctx.method_node(rel, c, m.name if m else None)
    tgt = ctx.class_node(msg, rel)
    if ctx.em.edge(src, tgt, REL_REFS, kind="handles_message", confidence=conf, source_file=rel, line=line):
        ctx.stats["edges.messenger_handler"] += 1


def _listener(ctx: Ctx, rel, c, method_name, event, line, kind="listens_to", conf="EXTRACTED") -> None:
    ev = _event_node(ctx, event, rel)
    if not ev:
        return
    mname = method_name
    if not mname:
        evs = event if isinstance(event, str) else (event[2] if isinstance(event, tuple) and event[0] == "const" else
                                                    (event[1] if isinstance(event, tuple) else ""))
        last = re.split(r"[.\\]", str(evs))[-1]
        for cand in (_camel(str(evs)), last, "__invoke"):
            if c.method(cand):
                mname = cand
                break
    src = ctx.method_node(rel, c, mname)
    if ctx.em.edge(src, ev, REL_REFS, kind=kind, confidence=conf, source_file=rel, line=line):
        ctx.stats["edges." + kind] += 1


def extract_wiring(ctx: Ctx) -> None:
    for rel, pf, c in list(_app_classes(ctx)):
        if not pf.parsed:
            continue
        for a in c.attrs:
            if a.short == "AsCommand":
                name = _str(a.kwargs.get("name")) or (_str(a.args[0]) if a.args else None)
                if name:
                    _command(ctx, rel, c, name, a.line)
            elif a.short == "AsMessageHandler":
                _handler(ctx, rel, pf, c, _str(a.kwargs.get("method")), _class_ref(a.kwargs.get("handles")), a.line)
            elif a.short == "AsEventListener":
                ev = a.kwargs.get("event", a.args[0] if a.args else None)
                _listener(ctx, rel, c, _str(a.kwargs.get("method")), ev, a.line)
            elif a.short in ("AsDoctrineListener", "AsEntityListener"):
                ev = a.kwargs.get("event", a.args[0] if a.args else None)
                if isinstance(ev, str) and not ev.startswith("doctrine."):
                    ev = "doctrine." + ev
                _listener(ctx, rel, c, _str(a.kwargs.get("method")), ev, a.line, kind="doctrine_listener")
                ent = _class_ref(a.kwargs.get("entity"))
                if ent:
                    ctx.em.edge(ctx.index.class_id(rel, c.name), ctx.class_node(ent, rel), REL_REFS,
                                kind="entity_listener", source_file=rel, line=a.line)
            elif a.short == "AsDecorator":
                dec = a.kwargs.get("decorates", a.args[0] if a.args else None)
                dfq = _class_ref(dec) or (_str(dec) if _str(dec) and "\\" in _str(dec) else None)
                if dfq:
                    if ctx.em.edge(ctx.index.class_id(rel, c.name), ctx.class_node(dfq, rel), REL_USES,
                                   kind="decorates", source_file=rel, line=a.line):
                        ctx.stats["edges.decorates"] += 1
        for m in c.methods:
            for a in m.attrs:
                if a.short == "AsMessageHandler":
                    _handler(ctx, rel, pf, c, m.name, _class_ref(a.kwargs.get("handles")), a.line)
                elif a.short == "AsEventListener":
                    ev = a.kwargs.get("event", a.args[0] if a.args else None)
                    if ev is None:
                        t = _first_param_type(pf, m)
                        ev = ("class", t) if t else None
                    _listener(ctx, rel, c, m.name, ev, a.line)
        if any(i.rsplit("\\", 1)[-1] == "MessageHandlerInterface" for i in c.implements):
            _handler(ctx, rel, pf, c, "__invoke", None, c.line)
        for line, ev in c.subscribed_events:
            evv: object = ev
            if "::" in ev:
                cls, const = ev.split("::", 1)
                evv = ("const", cls, const)
            elif "\\" in ev:
                evv = ("class", ev)
            _listener(ctx, rel, c, None, evv, line, kind="subscribes_to")


# ================================================================ container
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class _Svc:
    id: str
    cls: str | None = None
    alias: str | None = None
    tags: list = field(default_factory=list)
    refs: list = field(default_factory=list)  # (service id, kind)


def parse_container_xml(path: Path) -> dict[str, _Svc]:
    import xml.etree.ElementTree as ET

    services: dict[str, _Svc] = {}
    try:
        tree = ET.parse(path)
    except Exception:
        return services
    root = tree.getroot()
    svcs = None
    for child in root:
        if _strip_ns(child.tag) == "services":
            svcs = child
    if svcs is None:
        return services

    def collect(el, out, kind="injects"):
        for arg in el:
            t = _strip_ns(arg.tag)
            if t not in ("argument", "call", "property", "factory", "configurator"):
                continue
            typ = arg.get("type")
            if t == "call":
                collect(arg, out, "calls")
            elif typ == "service" and arg.get("id"):
                out.append((arg.get("id"), kind))
            elif typ in ("collection", "iterator"):
                collect(arg, out, "injects_iterator" if typ == "iterator" else kind)
            elif t == "factory" and arg.get("service"):
                out.append((arg.get("service"), "factory"))

    for el in svcs:
        if _strip_ns(el.tag) != "service" or not el.get("id"):
            continue
        s = _Svc(id=el.get("id"), cls=el.get("class"), alias=el.get("alias"))
        for sub in el:
            if _strip_ns(sub.tag) == "tag":
                s.tags.append((sub.get("name"), dict(sub.attrib)))
        collect(el, s.refs)
        services[s.id] = s
    return services


def _boot_container(ctx: Ctx, app: App) -> None:
    import subprocess

    console = app.root / "bin" / "console"
    if not console.is_file():
        ctx.notes.append(f"[{app.label}] --boot-container: no bin/console")
        return
    try:
        r = subprocess.run(["php", "bin/console", "cache:warmup", "--env=dev", "--no-interaction"],
                           cwd=app.root, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "APP_ENV": "dev"})
        if r.returncode != 0:
            ctx.notes.append(f"[{app.label}] cache:warmup failed (exit {r.returncode}): "
                             f"{(r.stderr or r.stdout).strip().splitlines()[-1:] }")
    except Exception as exc:  # timeout, php missing, ...
        ctx.notes.append(f"[{app.label}] cache:warmup failed: {exc}")


def extract_container(ctx: Ctx) -> None:
    for app in ctx.project.apps:
        if ctx.boot_container:
            _boot_container(ctx, app)
        dumps = container_dumps(app)
        if not dumps:
            ctx.notes.append(f"[{app.label}] no var/cache/dev/*DebugContainer.xml - DI container skipped "
                             "(warm the dev cache, or pass --boot-container)")
            continue
        dump = max(dumps, key=lambda p: p.stat().st_mtime)
        t = time.perf_counter()
        services = parse_container_xml(dump)
        ctx.stats["container_services"] += len(services)
        # freshness
        dmt = dump.stat().st_mtime
        newer = 0
        for rel in ctx.php:
            if app.rel and not rel.startswith(app.rel + "/"):
                continue
            try:
                if os.path.getmtime(ctx.abs(rel)) > dmt + 1:
                    newer += 1
            except OSError:
                pass
        for cfg in (app.root / "config").rglob("*.yaml") if (app.root / "config").is_dir() else []:
            try:
                if cfg.stat().st_mtime > dmt + 1:
                    newer += 1
            except OSError:
                pass
        if newer:
            ctx.notes.append(f"[{app.label}] container dump {dump.name} is older than {newer} source/config "
                             "file(s); DI edges may be stale")

        def final(sid: str, depth=0) -> _Svc | None:
            s = services.get(sid)
            if s is None:
                if sid.endswith(".inner"):
                    return None
                return None
            if s.alias and depth < 10:
                return final(s.alias, depth + 1)
            return s

        near = app.rel + "/" if app.rel else ""

        def app_class(fq: str | None):
            if not fq or "\\" not in fq:
                return None
            hit = ctx.find_class(fq, near)
            return hit

        rev_alias: dict[str, list[str]] = defaultdict(list)
        for sid0, s0 in services.items():
            if s0.alias and "\\" in sid0 and " $" not in sid0:
                f0 = final(sid0)
                if f0:
                    rev_alias[f0.id].append(sid0)

        def node_for(sid: str, from_rel: str | None = None) -> str | None:
            s = final(sid)
            fq = s.cls if s else None
            if fq:
                n = ctx.class_node(fq, near)
                if n:
                    return n
            if "\\" in sid:
                n = ctx.class_node(sid, near)
                if n:
                    return n
            if s is not None and from_rel:
                # vendor service: point at the interface/class the consumer imported
                # that the container aliases to this service (e.g. EntityManagerInterface)
                imported = ctx.index.imports.get(ctx.index.file_id(from_rel) or "", set())
                for alias in rev_alias.get(s.id, ()):
                    n = ctx.class_node(alias, near)
                    if n and n in imported:
                        return n
            return None

        for sid, s in services.items():
            if s.alias:
                # interface / FQCN alias bound to an application service
                if "\\" in sid and not sid.startswith("."):
                    tgt = final(s.alias)
                    if tgt and app_class(tgt.cls) and tgt.cls != sid:
                        src = ctx.class_node(sid.split(" $")[0], near)
                        dst = node_for(s.alias)
                        if ctx.em.edge(src, dst, REL_USES, kind="di_alias", source_file=None,
                                       sf_service_id=sid):
                            ctx.stats["edges.di_alias"] += 1
                continue
            hit = app_class(s.cls)
            if not hit:
                continue
            rel, cls = hit
            cid = ctx.index.class_id(rel, cls.name)
            if not cid:
                continue
            ctx.stats["container_app_services"] += 1
            ctor_line = cls.line
            pf = ctx.php.get(rel)
            if pf and pf.parsed:
                cm = cls.method("__construct")
                ctor_line = cm.line if cm else cls.line
            # named service id
            if s.id != s.cls and "\\" not in s.id and not s.id.startswith(".") and s.id != "kernel":
                nid = scoped_id(rel, "service", s.id)
                ctx.em.node(nid, f"service {s.id}", sf_type="service", source_file=rel, line=cls.line,
                            file_type="concept", sf_service_id=s.id)
                if ctx.em.edge(nid, cid, REL_REFS, kind="service_class", source_file=rel, line=cls.line):
                    ctx.stats["edges.service_class"] += 1
            for ref, kind in s.refs:
                if ref.startswith(".service_locator") or ref == "service_container":
                    continue
                tgt = node_for(ref, rel)
                if not tgt:
                    ctx.miss("di_vendor_refs_dropped", ref)
                    continue
                if kind == "injects_iterator" and not app_class((final(ref) or _Svc(ref)).cls):
                    continue
                if ctx.em.edge(cid, tgt, REL_USES, kind=kind if kind != "calls" else "injects",
                               source_file=rel, line=ctor_line, sf_service_id=ref):
                    ctx.stats["edges.di_" + kind] += 1
            for tname, tattrs in s.tags:
                if tname == "kernel.event_listener" and tattrs.get("event"):
                    _listener(ctx, rel, cls, tattrs.get("method") or None, tattrs["event"], cls.line)
                elif tname == "console.command" and tattrs.get("command"):
                    _command(ctx, rel, cls, tattrs["command"], cls.line)
                elif tname == "messenger.message_handler":
                    pff = ctx.php_full(rel)
                    c2 = next((x for x in (pff.classes if pff else []) if x.name == cls.name), cls)
                    handles = tattrs.get("handles") or None
                    _handler(ctx, rel, pff or pf, c2, tattrs.get("method") or None, handles, c2.line)
                elif tname in ("doctrine.event_listener", "doctrine.event_subscriber") and tattrs.get("event"):
                    _listener(ctx, rel, cls, tattrs.get("method") or None, "doctrine." + tattrs["event"],
                              cls.line, kind="doctrine_listener")
                elif tname == "doctrine.orm.entity_listener":
                    ent = tattrs.get("entity")
                    if ent:
                        ctx.em.edge(cid, ctx.class_node(ent, rel), REL_REFS, kind="entity_listener",
                                    source_file=rel, line=cls.line)
                    if tattrs.get("event"):
                        _listener(ctx, rel, cls, tattrs.get("method") or None, "doctrine." + tattrs["event"],
                                  cls.line, kind="doctrine_listener")
                elif tname == "container.decorator" and tattrs.get("id"):
                    tgt = node_for(tattrs.get("inner") or "") or node_for(tattrs["id"])
                    if ctx.em.edge(cid, tgt, REL_USES, kind="decorates", source_file=rel, line=cls.line,
                                   sf_service_id=tattrs["id"]):
                        ctx.stats["edges.decorates"] += 1
        ctx.timings[f"container[{app.label}]"] = time.perf_counter() - t


# ================================================================ driver
def run_all(ctx: Ctx) -> None:
    build_php_index(ctx)
    collect_templates(ctx)
    steps = [
        ("templates", extract_templates),
        ("php_templates", extract_php_templates),
        ("components", extract_components),
        ("stimulus", extract_stimulus),
        ("routes", extract_routes),
        ("doctrine", extract_doctrine),
        ("wiring", extract_wiring),
        ("container", extract_container),
    ]
    for name, fn in steps:
        t = time.perf_counter()
        fn(ctx)
        ctx.timings.setdefault(name, time.perf_counter() - t)
