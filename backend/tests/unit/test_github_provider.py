import json

import pytest
from fakes import RecordingRunner

from pr_explorer.application.ports import AnalysisRequest, SourceError
from pr_explorer.infrastructure.source.git_cache import GitCache
from pr_explorer.infrastructure.source.providers import GitHubSourceProvider, parse_pr_url

HEAD = "a" * 40
BASE = "b" * 40
MB = "c" * 40


def handler(args, _input):
    if args[0] == "gh":
        fields = args[args.index("--json") + 1]
        if fields == "headRefOid":
            return json.dumps({"headRefOid": "d" * 40})
        return json.dumps({"number": 7, "title": "Fix", "body": "b", "author": {"login": "dev"}, "baseRefName": "main",
                           "headRefName": "fix", "baseRefOid": BASE, "headRefOid": HEAD, "url": "https://github.com/o/r/pull/7"})
    if "cat-file" in args:
        return "commit\n"
    if "merge-base" in args:
        return MB + "\n"
    return ""


def test_parse_pr_url():
    assert parse_pr_url("https://github.com/acme/shop/pull/123") == ("acme", "shop", 123)
    for bad in ["https://gitlab.com/a/b/pull/1", "https://github.com/a/b/issues/1", "github.com/a/b/pull/1", "https://github.com/a/b/pull/x"]:
        with pytest.raises(SourceError):
            parse_pr_url(bad)


def test_resolve_uses_gh_and_fetches_pull_head_without_network(tmp_path):
    runner = RecordingRunner(handler)
    p = GitHubSourceProvider(GitCache(tmp_path, runner), runner)
    pr = p.resolve(AnalysisRequest(pr_url="https://github.com/o/r/pull/7"))
    assert (pr.head_sha, pr.base_sha, pr.merge_base_sha, pr.number, pr.author) == (HEAD, BASE, MB, 7, "dev")
    argvs = [c["args"] for c in runner.calls]
    assert argvs[0][:4] == ["gh", "pr", "view", "7"] and "--repo" in argvs[0]
    clone = next(a for a in argvs if "clone" in a)
    assert "--bare" in clone and "https://github.com/o/r.git" in clone
    fetch = next(a for a in argvs if "fetch" in a)
    assert "+refs/pull/7/head:refs/pr/7" in fetch
    assert all("core.hooksPath=/dev/null" in a for a in argvs if a[0] == "git")
    assert p.current_head_sha(pr.descriptor) == "d" * 40


def test_gh_failure_is_source_error(tmp_path):
    from pr_explorer.infrastructure.process.runner import RunResult

    runner = RecordingRunner(lambda a, i: RunResult(1, "", "HTTP 404"))
    with pytest.raises(SourceError, match="gh pr view failed"):
        GitHubSourceProvider(GitCache(tmp_path, runner), runner).resolve(AnalysisRequest(pr_url="https://github.com/o/r/pull/7"))
