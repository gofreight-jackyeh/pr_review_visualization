"""`pr-explorer serve` / `pr-explorer analyze --local REPO --base B --head H [--semantic off]`.

`analyze` prints two paths: the system map (shared by every PR on the merge-base commit) and the PR overlay."""

from __future__ import annotations

import argparse
import sys

from ..domain.model.semantic import AiChoice
from ..application.ports import AnalysisRequest, LocalSpec
from ..application.use_cases import AppError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pr-explorer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app (default 0.0.0.0:8766)")
    s.add_argument("--port", type=int, default=8766)
    s.add_argument("--host", default="0.0.0.0", help="bind address; 127.0.0.1 = this machine only")
    a = sub.add_parser("analyze", help="analyze a PR and print the system map and overlay paths")
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("--local", metavar="REPO", help="local git repository path")
    src.add_argument("--pr-url", help="GitHub PR URL (uses your gh/git auth)")
    a.add_argument("--base")
    a.add_argument("--head")
    a.add_argument("--title")
    a.add_argument("--body")
    a.add_argument("--semantic", default="auto", choices=["auto", "off", "light", "standard", "deep"])
    a.add_argument("--engine", choices=["claude", "codex"], help="AI for the PR texts (default: the server default)")
    a.add_argument("--model", help="model of that engine (default: its own setting)")
    a.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    a.add_argument("--dry-run-semantic", metavar="DIR",
                   help="do not call Claude: write every prompt to DIR (sizes are in the stage messages)")
    args = ap.parse_args(argv)

    from ..bootstrap import build_container

    semantic = None
    if getattr(args, "dry_run_semantic", None):
        from ..infrastructure.semantic.dry_run import DryRunSemanticAnalyzer

        semantic = DryRunSemanticAnalyzer(args.dry_run_semantic)
    c = build_container(semantic=semantic)
    if args.cmd == "serve":
        from .http.app import serve

        serve(c, args.port, args.host)
        return 0
    if args.local and not (args.base and args.head):
        ap.error("--local requires --base and --head")
    import os

    req = AnalysisRequest(
        pr_url=args.pr_url,
        local=LocalSpec(os.path.abspath(args.local), args.base, args.head, args.title, args.body) if args.local else None,
        semantic_mode=args.semantic,
        ai=AiChoice(args.engine or c.analyze.ai_default.engine, args.model, args.effort)
        if (args.engine or args.model or args.effort) else None,
    )
    try:
        paths, rec = c.analyze.analyze_sync(req)
    except (AppError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"system_map: {paths['system_map']}")
    print(f"overlay:    {paths['overlay']}")
    for s in rec.get("stages", []):
        print(f"  {s['name']:<11} {s['status']:<8} {s['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
