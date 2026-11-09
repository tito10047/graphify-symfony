"""graphify-symfony command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__
from . import hooks as hookmod
from .detect import detect_project
from .graph import ORIGIN, Emitter, GraphIndex, graphify_lock, load_graph, strip_ours, write_graph_atomic

PENDING = (".pending_changes", ".pending_full", ".pending_incremental")


def _out_dir(root: Path) -> Path:
    out = os.environ.get("GRAPHIFY_OUT", "graphify-out")
    p = Path(out)
    return p if p.is_absolute() else root / p


def _ignored_predicate(root: Path):
    try:
        from graphify.detect import ignored_predicate  # type: ignore

        return ignored_predicate(root)
    except Exception:
        return None


def _wait_for_graphify(out: Path, graph: Path, since: float, timeout: float) -> str:
    """Hook mode: give graphify's detached rebuild time to start (it takes
    .rebuild.lock); returns once it started or ``timeout`` passed."""
    lock = out / ".rebuild.lock"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lock.exists():
            return "graphify rebuild running"
        try:
            if graph.stat().st_mtime > since:
                return "graph.json already rewritten"
        except OSError:
            pass
        time.sleep(0.5)
    return "graphify rebuild did not start (nothing to rebuild?)"


def _stat_sig(p: Path):
    st = p.stat()
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _extract(root: Path, graph: dict, args, project) -> tuple[dict, Counter, list[str], dict]:
    from .extract import Ctx, run_all

    removed_n, removed_e = strip_ours(graph)
    index = GraphIndex(graph)
    em = Emitter(index)
    ctx = Ctx(project=project, index=index, em=em, boot_container=getattr(args, "boot_container", False))
    ctx.ignored = _ignored_predicate(root)
    run_all(ctx)
    added_n, added_e = em.merge_into(graph)
    ctx.stats["removed_nodes"] = removed_n
    ctx.stats["removed_edges"] = removed_e
    ctx.stats["added_nodes"] = added_n
    ctx.stats["added_edges"] = added_e
    ctx.stats["dropped_edges_missing_endpoint"] = em.dropped_edges
    by_type = Counter(n["sf_type"] for n in graph["nodes"] if n.get("_origin") == ORIGIN)
    lk = "links" if "links" in graph else "edges"
    by_kind = Counter(e["sf_kind"] for e in graph[lk] if e.get("_origin") == ORIGIN)
    return graph, ctx.stats, ctx.notes, {"nodes_by_type": dict(by_type), "edges_by_kind": dict(by_kind),
                                         "unresolved_samples": dict(ctx.samples),
                                         "timings": {k: round(v, 3) for k, v in ctx.timings.items()}}


def cmd_run(args) -> int:
    t0 = time.perf_counter()
    root = Path(args.root).resolve()
    out = _out_dir(root)
    gpath = out / "graph.json"
    if args.after_graphify:
        print(f"[graphify-symfony] {time.strftime('%F %T')} hook run in {root}", flush=True)
        print(f"[graphify-symfony] {_wait_for_graphify(out, gpath, args.since or time.time() - 2, args.wait)}",
              flush=True)
        time.sleep(0.2)
        with graphify_lock(out, blocking=True):
            pass  # wait for graphify's rebuild to finish
    if not gpath.is_file():
        print(f"[graphify-symfony] no {gpath} - run `graphify update {root}` (or `graphify extract`) first")
        return 2 if args.strict else 0
    project = detect_project(root)
    result = {}
    for attempt in range(3):
        sig = _stat_sig(gpath)
        graph = load_graph(gpath)
        if not project.is_symfony:
            n, e = strip_ours(graph)
            if n or e:
                with graphify_lock(out, blocking=True):
                    if _stat_sig(gpath) == sig:
                        write_graph_atomic(gpath, graph)
            print(f"[graphify-symfony] {root}: not a Symfony project (no composer.json requiring "
                  f"symfony/framework-bundle with bin/console or config/bundles.php) - nothing to do"
                  + (f"; removed {n} stale nodes" if n else ""))
            return 0
        graph, stats, notes, extra = _extract(root, graph, args, project)
        payload = json.dumps(graph, ensure_ascii=True, separators=(",", ":"))
        written = False
        retry = False
        with graphify_lock(out, blocking=True):
            if _stat_sig(gpath) != sig:
                retry = True  # graphify rewrote it while we were extracting
            else:
                with open(gpath, "rb") as fh:
                    same = fh.read() == payload.encode("utf-8")
                if not same and not args.dry_run:
                    write_graph_atomic(gpath, graph)
                    written = True
        if retry:
            print("[graphify-symfony] graph.json changed during extraction, retrying", flush=True)
            continue
        drained = False
        if args.after_graphify and any((out / p).exists() for p in PENDING) and attempt < 2:
            # we held graphify's lock while a hook rebuild tried to start: let it catch up
            try:
                from graphify.watch import _rebuild_code  # type: ignore

                cwd = os.getcwd()
                os.chdir(root)
                try:
                    _rebuild_code(Path("."), changed_paths=[], block_on_lock=True)
                finally:
                    os.chdir(cwd)
                drained = True
            except Exception as exc:
                print(f"[graphify-symfony] could not drain graphify's pending changes: {exc}")
        result = {
            "root": str(root),
            "apps": [a.label for a in project.apps],
            "written": written,
            "stats": dict(sorted(stats.items())),
            **extra,
            "notes": notes,
            "seconds": round(time.perf_counter() - t0, 2),
        }
        if drained:
            continue
        break
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        s = result.get("stats", {})
        print(f"[graphify-symfony] {root}: apps={','.join(result.get('apps', []))} "
              f"+{s.get('added_nodes', 0)} nodes +{s.get('added_edges', 0)} edges "
              f"(replaced {s.get('removed_nodes', 0)}/{s.get('removed_edges', 0)}) "
              f"{'written' if result.get('written') else 'unchanged'} in {result.get('seconds')}s")
        if args.verbose:
            print("  nodes:", json.dumps(result.get("nodes_by_type", {}), sort_keys=True))
            print("  edges:", json.dumps(result.get("edges_by_kind", {}), sort_keys=True))
            print("  timings:", json.dumps(result.get("timings", {})))
        for n in result.get("notes", []):
            print(f"  note: {n}")
    return 0


def cmd_detect(args) -> int:
    project = detect_project(Path(args.root))
    if args.json:
        print(json.dumps(project.to_json(), indent=2))
    else:
        if not project.is_symfony:
            print(f"{project.root}: not a Symfony project")
        for a in project.apps:
            print(f"app {a.label} ({a.name})")
            for ns, d in a.template_dirs:
                print(f"  templates {'@' + ns if ns else '(main)'}: {d}")
            if a.sdc_dir:
                print(f"  ux-sdc: {a.sdc_dir} ({a.sdc_namespace})")
            from .detect import container_dumps

            for d in container_dumps(a):
                print(f"  container dump: {d.relative_to(project.root)}")
    return 0 if project.is_symfony or not args.strict else 1


def cmd_strip(args) -> int:
    root = Path(args.root).resolve()
    out = _out_dir(root)
    gpath = out / "graph.json"
    if not gpath.is_file():
        print(f"no {gpath}")
        return 0
    with graphify_lock(out, blocking=True):
        graph = load_graph(gpath)
        n, e = strip_ours(graph)
        if n or e:
            write_graph_atomic(gpath, graph)
    print(f"removed {n} nodes, {e} edges added by graphify-symfony")
    return 0


def cmd_hook(args) -> int:
    root = Path(args.root)
    if args.action == "install":
        print(hookmod.install(root, with_graphify=not args.no_graphify))
        return 0
    if args.action == "uninstall":
        print(hookmod.uninstall(root))
        return 0
    text, ok = hookmod.status(root)
    print(text)
    return 0 if ok else 1


def cmd_setup(args) -> int:
    from .setup import setup

    log = (lambda *a, **k: print(*a, file=sys.stderr, **k)) if args.json else print
    try:
        res = setup(Path(args.root), claude_hooks=args.claude_hooks, git_hooks=not args.no_git_hooks,
                    build=not args.no_build, log=log)
    except Exception as exc:
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for rel, status in (res.get("files") or {}).items():
            print(f"  {rel}: {status}")
        if res.get("git_hooks"):
            print("  git hooks:\n    " + str(res["git_hooks"]).replace("\n", "\n    "))
        if res.get("build"):
            print(f"  graph: {res['build']}")
        if res.get("error"):
            print(f"error: {res['error']}", file=sys.stderr)
        if res.get("ok"):
            print("Commit .graphifyignore, .gitignore and CLAUDE.md; graphify-out/ stays local.")
    return 0 if res.get("ok") else 1


def cmd_teardown(args) -> int:
    from .setup import teardown

    res = teardown(Path(args.root), purge=args.purge)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for rel, status in res["files"].items():
            print(f"  {rel}: {status}")
        if res.get("purged"):
            print(f"  removed {res['purged']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="graphify-symfony", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="add Symfony nodes/edges to graphify-out/graph.json (idempotent)")
    r.add_argument("--root", default=".")
    r.add_argument("--strict", action="store_true", help="exit 2 when graph.json is missing")
    r.add_argument("--boot-container", action="store_true",
                   help="run `php bin/console cache:warmup` first (EXECUTES project code, 60 s timeout)")
    r.add_argument("--json", action="store_true", help="print machine-readable stats")
    r.add_argument("-v", "--verbose", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="extract but do not write graph.json")
    r.add_argument("--after-graphify", action="store_true",
                   help="hook mode: wait for graphify's background rebuild first")
    r.add_argument("--wait", type=float, default=float(os.environ.get("GRAPHIFY_SYMFONY_WAIT", "60")),
                   help="hook mode: seconds to wait for graphify's rebuild to start (default 60)")
    r.add_argument("--since", type=float, default=None, help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("detect", help="report Symfony app roots")
    d.add_argument("--root", default=".")
    d.add_argument("--json", action="store_true")
    d.add_argument("--strict", action="store_true", help="exit 1 when not a Symfony project")
    d.set_defaults(fn=cmd_detect)

    s = sub.add_parser("strip", help="remove everything graphify-symfony added")
    s.add_argument("--root", default=".")
    s.set_defaults(fn=cmd_strip)

    h = sub.add_parser("hook", help="chain the post-pass into graphify's git hooks")
    h.add_argument("action", choices=["install", "uninstall", "status"])
    h.add_argument("--root", default=".")
    h.add_argument("--no-graphify", action="store_true", help="install: do not (re)install graphify's own hooks")
    h.set_defaults(fn=cmd_hook)

    su = sub.add_parser("setup", help="one-shot per-project setup (ignore files, CLAUDE.md, hooks, first build)")
    su.add_argument("--root", default=".")
    su.add_argument("--claude-hooks", choices=["local", "shared", "none"], default="local",
                    help="where graphify's Claude Code hooks go: .claude/settings.local.json (default), "
                         ".claude/settings.json, or nowhere")
    su.add_argument("--no-git-hooks", action="store_true")
    su.add_argument("--no-build", action="store_true", help="do not build the graph")
    su.add_argument("--json", action="store_true")
    su.set_defaults(fn=cmd_setup)

    td = sub.add_parser("teardown", help="undo `setup` (restores files byte-exactly where possible)")
    td.add_argument("--root", default=".")
    td.add_argument("--purge", action="store_true", help="also delete graphify-out/")
    td.add_argument("--json", action="store_true")
    td.set_defaults(fn=cmd_teardown)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
