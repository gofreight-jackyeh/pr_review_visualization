from pr_explorer.domain.model.graph import Edge, Node
from pr_explorer.domain.services.impact_propagation import propagate_impact


def n(i, status="unchanged", parent=None):
    return Node(i, "function", "symbol", i, parent, change_status=status)


def e(src, dst, kind="calls", conf="confirmed"):
    return Edge(f"{kind}:{src}->{dst}", src, dst, kind, conf, ["ev"])


def run(nodes, edges, **kw):
    nd = {x.id: x for x in nodes}
    propagate_impact(nd, {x.id: x for x in edges}, **kw)
    return nd


def test_statuses_by_evidence():
    nd = run(
        [n("changed", "modified"), n("caller"), n("importer"), n("grand")],
        [e("caller", "changed"), e("importer", "changed", "imports"), e("grand", "caller")],
    )
    assert nd["changed"].impact_status == "direct"
    assert nd["caller"].impact_status == "evidence_supported"  # calls the changed function itself
    assert nd["importer"].impact_status == "confirmed_dependency"  # import only, no proof the change is used
    assert nd["grand"].impact_status == "potential" and nd["grand"].impact_depth == 2


def test_container_target_is_confirmed_dependency_not_evidence():
    nd = run([n("cls", "modified"), n("m", "modified", parent="cls"), n("user")], [e("user", "cls")])
    assert nd["user"].impact_status == "confirmed_dependency"


def test_bounded_depth_is_honored():
    chain = [n("c0", "modified")] + [n(f"c{i}") for i in range(1, 7)]
    edges = [e(f"c{i}", f"c{i - 1}") for i in range(1, 7)]
    nd = run(chain, edges, max_depth=3)
    assert [nd[f"c{i}"].impact_status for i in range(1, 7)] == [
        "evidence_supported", "potential", "potential", "none", "none", "none"]


def test_max_nodes_budget():
    nodes = [n("x", "modified")] + [n(f"d{i}") for i in range(10)]
    nd = {x.id: x for x in nodes}
    res = propagate_impact(nd, {f"{i}": e(f"d{i}", "x") for i in range(10)}, max_nodes=4)
    assert res.truncated and sum(1 for v in nd.values() if v.impact_status not in ("none", "direct")) == 4


def test_unknown_only_reachability_is_unknown():
    nd = run(
        [n("changed", "modified"), n("dyn"), n("dyn_parent"), n("both")],
        [e("dyn", "changed", conf="unknown"), e("dyn_parent", "dyn"), e("both", "changed", conf="unknown"), e("both", "changed", "imports")],
    )
    assert nd["dyn"].impact_status == "unknown"
    assert nd["dyn_parent"].impact_status == "unknown"  # reachable only through an unknown edge
    assert nd["both"].impact_status == "confirmed_dependency"  # a confirmed path wins


def test_removed_edges_do_not_propagate_and_externals_are_not_seeds():
    ext = Node("ext", "external", "symbol", "ext", None, change_status="added")
    nd = run([n("changed", "modified"), n("old"), ext, n("caller")],
             [Edge("r", "old", "changed", "calls", "confirmed", ["ev"], "removed"), e("caller", "ext", conf="unknown")])
    assert nd["old"].impact_status == "none"
    assert nd["ext"].impact_status == "none" and nd["caller"].impact_status == "none"
