import pytest

from pr_explorer.domain.model.changes import ChangedFile
from pr_explorer.domain.model.graph import Graph, Node, SideGraph
from pr_explorer.domain.services.complexity_score import decide, score_change


def f(path, lang="python", adds=3):
    return ChangedFile(f"file:{path}", path, "modified", lang, adds, 1)


def g(*nodes):
    return Graph({n.id: n for n in nodes}, {}, {}, SideGraph("base"), SideGraph("head"))


def test_docs_only_is_skipped():
    r = score_change([f("README.md", "markdown"), f("docs/guide.md", "markdown")], g())
    d, tier = decide("auto", r)
    assert tier == "none" and r.score == 0 and "documentation-only" in r.reasons[0]


def test_formatting_only_is_none():
    r = score_change([f("a.py")], g(Node("m", "module", "module", "a", None)))
    assert decide("auto", r)[1] == "none"


@pytest.mark.parametrize("kind", ["model", "view", "route"])
def test_model_or_api_change_is_at_least_standard(kind):
    r = score_change([f("app/models.py")], g(Node("x", kind, "symbol", "X", None, change_status="modified")))
    assert decide("auto", r)[1] in {"standard", "deep"}


def test_manual_override_wins_and_is_recorded():
    docs = score_change([f("README.md", "markdown")], g())
    big = score_change([f("m.py")], g(*[Node(f"m{i}", "model", "symbol", f"M{i}", None, change_status="added") for i in range(5)]))
    assert decide("deep", docs) [1] == "deep" and decide("deep", docs)[0].mode == "deep"
    assert decide("light", big)[1] == "light" and decide("light", big)[0].mode == "light"
    d, tier = decide("off", big)
    assert tier == "none" and d.mode == "off"
    with pytest.raises(ValueError):
        decide("bogus", big)
