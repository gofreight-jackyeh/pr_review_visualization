"""Source providers: GitHub (gh CLI + git) and local repo (+ a router choosing between them)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...application.ports import AnalysisRequest, ResolvedPr, SourceError, Workspace
from ..process.runner import CommandRunner
from .git_cache import SHA_RE, GitCache, git_env

PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?(?:[?#].*)?$")
GH_FIELDS = "number,title,body,author,baseRefName,headRefName,baseRefOid,headRefOid,url"


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL.match(url.strip())
    if not m:
        raise SourceError(f"not a GitHub pull request URL: {url!r}")
    return m.group(1), m.group(2).removesuffix(".git"), int(m.group(3))


def _materialize(cache: GitCache, pr: ResolvedPr) -> Workspace:
    """Holds both worktrees until `release(pr)` (one hold per materialize / retain call)."""
    base_root = cache.worktree(pr.repo_key, pr.merge_base_sha)
    try:
        head_root = cache.worktree(pr.repo_key, pr.head_sha)
    except BaseException:
        cache.release_worktree(pr.repo_key, pr.merge_base_sha)
        raise
    try:
        return Workspace(
            base_root=base_root, head_root=head_root,
            diff_text=cache.diff(pr.repo_key, pr.merge_base_sha, pr.head_sha),
            base_files=cache.ls_tree(pr.repo_key, pr.merge_base_sha),
            head_files=cache.ls_tree(pr.repo_key, pr.head_sha),
        )
    except BaseException:
        _release(cache, pr)
        raise


def _release(cache: GitCache, pr: ResolvedPr) -> None:
    cache.release_worktree(pr.repo_key, pr.merge_base_sha)
    cache.release_worktree(pr.repo_key, pr.head_sha)


class LocalSourceProvider:
    """repo_path + base/head refs. Reads refs from the user's repo; checks out only in the cache."""

    def __init__(self, cache: GitCache, runner: CommandRunner):
        self.cache = cache
        self.runner = runner

    def _user_rev_parse(self, repo: str, ref: str) -> str:
        res = self.runner.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                              timeout=60, env=git_env())
        sha = res.stdout.strip()
        if res.returncode != 0 or not SHA_RE.match(sha):
            raise SourceError(f"cannot resolve {ref!r} in {repo}")
        return sha

    def resolve(self, request: AnalysisRequest) -> ResolvedPr:
        spec = request.local
        assert spec is not None
        repo = Path(spec.repo_path).expanduser()
        if not repo.is_absolute() or not repo.is_dir():
            raise SourceError(f"local repo_path must be an existing absolute directory: {spec.repo_path!r}")
        repo_s = str(repo.resolve())
        base_sha = self._user_rev_parse(repo_s, spec.base)
        head_sha = self._user_rev_parse(repo_s, spec.head)
        key = GitCache.key_for("local", repo_s, repo.name)
        self.cache.ensure_clone(key, repo_s)
        self.cache.fetch(key, repo_s, ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"])
        for sha in (base_sha, head_sha):
            if not self.cache.has_commit(key, sha):
                self.cache.fetch(key, repo_s, [sha])
        mb = self.cache.merge_base(key, base_sha, head_sha)
        title = spec.title or self.cache.log_subject(key, head_sha) or f"{spec.head} → {spec.base}"
        body = spec.body if spec.body is not None else self.cache.log_range(key, mb, head_sha)
        return ResolvedPr(
            source="local", repo_key=key, repo_name=repo.name, repo_owner=None, base_ref=spec.base, head_ref=spec.head,
            base_sha=base_sha, head_sha=head_sha, merge_base_sha=mb, title=title, body=body,
            author=self.cache.log_subject(key, head_sha, "%an") or None,
            descriptor={"kind": "local", "repo_path": repo_s, "head": spec.head},
        )

    def materialize(self, pr: ResolvedPr) -> Workspace:
        return _materialize(self.cache, pr)

    def current_head_sha(self, descriptor: dict[str, Any]) -> str:
        return self._user_rev_parse(descriptor["repo_path"], descriptor["head"])


class GitHubSourceProvider:
    """PR URL → `gh pr view` metadata + bare clone fetching pull/N/head. Uses the user's gh/git auth."""

    def __init__(self, cache: GitCache, runner: CommandRunner, gh_bin: str = "gh", timeout: float = 120):
        self.cache = cache
        self.runner = runner
        self.gh_bin = gh_bin
        self.timeout = timeout

    def _gh_view(self, owner: str, repo: str, number: int, fields: str = GH_FIELDS) -> dict:
        res = self.runner.run([self.gh_bin, "pr", "view", str(number), "--repo", f"{owner}/{repo}", "--json", fields],
                              timeout=self.timeout)
        if res.returncode != 0:
            raise SourceError(f"gh pr view failed: {res.stderr.strip()[:300]}")
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise SourceError(f"gh returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SourceError("gh returned unexpected payload")
        return data

    def resolve(self, request: AnalysisRequest) -> ResolvedPr:
        assert request.pr_url
        owner, repo, number = parse_pr_url(request.pr_url)
        meta = self._gh_view(owner, repo, number)
        head_sha = str(meta.get("headRefOid", ""))
        base_ref = str(meta.get("baseRefName", ""))
        if not SHA_RE.match(head_sha) or not base_ref:
            raise SourceError("gh pr view did not return head/base refs")
        key = GitCache.key_for("gh", f"{owner}/{repo}", f"{owner}-{repo}")
        remote = f"https://github.com/{owner}/{repo}.git"
        self.cache.ensure_clone(key, remote)
        self.cache.fetch(key, remote, [f"+refs/pull/{number}/head:refs/pr/{number}", f"+refs/heads/{base_ref}:refs/heads/{base_ref}"])
        if not self.cache.has_commit(key, head_sha):
            raise SourceError(f"head {head_sha[:10]} not present after fetch")
        base_sha = str(meta.get("baseRefOid") or "") or self.cache.rev_parse(key, f"refs/heads/{base_ref}")
        mb = self.cache.merge_base(key, base_sha, head_sha)
        author = meta.get("author") or {}
        return ResolvedPr(
            source="github", repo_key=key, repo_name=repo, repo_owner=owner, base_ref=base_ref,
            head_ref=str(meta.get("headRefName", "")), base_sha=base_sha, head_sha=head_sha, merge_base_sha=mb,
            title=str(meta.get("title", "")), body=str(meta.get("body") or ""), url=str(meta.get("url") or request.pr_url),
            number=number, author=author.get("login") if isinstance(author, dict) else None,
            descriptor={"kind": "github", "owner": owner, "repo": repo, "number": number},
        )

    def materialize(self, pr: ResolvedPr) -> Workspace:
        return _materialize(self.cache, pr)

    def current_head_sha(self, descriptor: dict[str, Any]) -> str:
        meta = self._gh_view(descriptor["owner"], descriptor["repo"], int(descriptor["number"]), "headRefOid")
        sha = str(meta.get("headRefOid", ""))
        if not SHA_RE.match(sha):
            raise SourceError("gh did not return headRefOid")
        return sha


class SourceRouter:
    def __init__(self, cache: GitCache, local: LocalSourceProvider, github: GitHubSourceProvider):
        self.cache = cache
        self.local = local
        self.github = github

    def _for(self, kind: str):
        return self.github if kind == "github" else self.local

    def resolve(self, request: AnalysisRequest) -> ResolvedPr:
        return self.github.resolve(request) if request.pr_url else self.local.resolve(request)

    def materialize(self, pr: ResolvedPr) -> Workspace:
        return self._for(pr.source).materialize(pr)

    def retain(self, pr: ResolvedPr) -> None:
        """One more hold on the PR's worktrees (e.g. a background Claude call that may outlive its analysis)."""
        self.cache.worktree(pr.repo_key, pr.merge_base_sha)
        self.cache.worktree(pr.repo_key, pr.head_sha)

    def release(self, pr: ResolvedPr) -> None:
        _release(self.cache, pr)

    def blobs(self, pr: ResolvedPr, sha: str) -> dict[str, str]:
        return self.cache.ls_tree_blobs(pr.repo_key, sha)

    def is_ancestor(self, pr: ResolvedPr, ancestor: str, sha: str) -> bool:
        return self.cache.is_ancestor(pr.repo_key, ancestor, sha)

    def diff(self, pr: ResolvedPr, a: str, b: str) -> str:
        """Zero-context diff between two cached commits (map reuse remaps evidence line numbers across it)."""
        return self.cache.git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", "-U0", a, b], key=pr.repo_key)

    def current_head_sha(self, descriptor: dict[str, Any]) -> str:
        return self._for(descriptor.get("kind", "local")).current_head_sha(descriptor)

