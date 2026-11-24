"""测试：模型、解析、分析、两台模拟器的一致性与事故复现。"""
from __future__ import annotations

import pytest

from cascade.analysis import analyze, enumerate_chains
from cascade.generator import build_company, build_incident
from cascade.model import Edge, EdgeConfig, Service, Topology
from cascade.recommend import build_proposals
from cascade.resolve import resolve_all, resolve_edge
from cascade.scenario import FaultSpec, Scenario, scenario_from_incident
from cascade.service import Service as AppService
from cascade.sim_des import run_des
from cascade.sim_stepped import run_stepped
from cascade.verify import amplification_bound, converged_check


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def company():
    return build_company()


@pytest.fixture(scope="module")
def resolved(company):
    return resolve_all(company)


@pytest.fixture(scope="module")
def analysis(company, resolved):
    return analyze(company, resolved)


def linear_topo(cfgs) -> Topology:
    """gateway -> a -> b，用给定配置。"""
    t = Topology(services={}, edges={}, seed=1, roots=["gateway"])
    t.add_service(Service("gateway", "gateway", 0, "edge", 1000, 1.0, slo_ms=3000))
    t.add_service(Service("a", "a", 1, "x", 100, 5.0))
    t.add_service(Service("b", "b", 2, "x", 100, 5.0))
    for eid, a, b, configs in cfgs:
        e = Edge(eid, a, b)
        e.configs = configs
        t.add_edge(e)
    return t


# ---------------------------------------------------------------------------
# 模型与解析
# ---------------------------------------------------------------------------
def test_generator_shape(company):
    assert len(company.services) >= 300
    assert len(company.edges) >= 300
    assert company.meta["incident"]["chain"] == [
        "edge-gateway-01", "order-bff-01", "order-svc-01", "inventory-svc-01"
    ]
    # 大部分边应当有多个来源（真实的乱）
    multi = sum(1 for e in company.edges.values() if len({c.source for c in e.configs}) >= 2)
    assert multi > 20, multi


def test_default_when_empty():
    e = Edge("e", "a", "b")
    re = resolve_edge(e)
    assert re.timeout_ms == 1000.0
    assert re.values["retries"] == 0
    assert re.origins["timeout_ms"].source == "default"
    assert re.origins["retries"].source == "default"


def test_config_center_overrides_code():
    e = Edge("e", "a", "b", [
        EdgeConfig("code", timeout_ms=1000.0, retries=1),
        EdgeConfig("config_center", timeout_ms=2000.0, retries=3),
    ])
    re = resolve_edge(e)
    # app 层内：配置中心优先于代码
    assert re.timeout_ms == 2000.0
    assert re.effective_retries == 3


def test_precise_beats_wildcard_same_source():
    e = Edge("e", "a", "b", [
        EdgeConfig("config_center", priority=5, timeout_ms=1000.0),
        EdgeConfig("config_center", priority=20, timeout_ms=2500.0),
    ])
    re = resolve_edge(e)
    assert re.timeout_ms == 2500.0


def test_timeout_takes_min_across_layers():
    e = Edge("e", "a", "b", [
        EdgeConfig("code", timeout_ms=5000.0),
        EdgeConfig("mesh", timeout_ms=2000.0),
    ])
    re = resolve_edge(e)
    assert re.timeout_ms == 2000.0  # min(5000, 2000)
    assert "min" in re.origins["timeout_ms"].label


def test_retries_multiply_across_layers():
    e = Edge("e", "a", "b", [
        EdgeConfig("code", retries=2),   # 3 次尝试
        EdgeConfig("mesh", retries=1),    # 2 次尝试
    ])
    re = resolve_edge(e)
    assert re.attempts == 6           # 3 × 2
    assert re.effective_retries == 5


def test_retries_zero_participates_in_product():
    e = Edge("e", "a", "b", [
        EdgeConfig("code", retries=0),
        EdgeConfig("mesh", retries=2),
    ])
    re = resolve_edge(e)
    assert re.app_retries == 0
    assert re.mesh_retries == 2
    assert re.attempts == 3           # (0+1)*(2+1)


def test_proposed_overrides_everything():
    e = Edge("e", "a", "b", [
        EdgeConfig("code", retries=3, timeout_ms=5000.0),
        EdgeConfig("mesh", retries=1, timeout_ms=3000.0),
        EdgeConfig("proposed", retries=0, timeout_ms=1500.0),
    ])
    re = resolve_edge(e)
    assert re.attempts == 1
    assert re.timeout_ms == 1500.0


def test_backoff_and_worst_case():
    e = Edge("e", "a", "b", [
        EdgeConfig("config_center", retries=2, timeout_ms=1000.0,
                   backoff_base_ms=100.0, backoff_mult=2.0, backoff_max_ms=10000.0),
    ])
    re = resolve_edge(e)
    assert re.backoff_ms(0) == 100.0
    assert re.backoff_ms(1) == 200.0
    assert re.worst_case_ms() == 3 * 1000.0 + 300.0


def test_incident_chain_config(company, resolved):
    bff = resolved["order-bff-01->order-svc-01"]
    assert bff.app_retries == 3
    assert bff.mesh_retries == 1
    assert bff.attempts == 8          # 4 × 2
    inv = resolved["order-svc-01->inventory-svc-01"]
    assert inv.attempts == 3


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------
def test_incident_amplification(analysis):
    assert analysis.incident is not None
    assert analysis.incident.leaf_amplification == 24   # 1 × 8 × 3


def test_incident_inversions(analysis):
    inc = analysis.incident
    assert "order-bff-01->order-svc-01" in inc.inverted_edges
    em = analysis.edges["order-bff-01->order-svc-01"]
    # 上游网关只有 3s，下游最坏要 49s
    assert em.upstream_budget_ms == 3000.0
    assert em.downstream_ms > em.upstream_budget_ms
    assert em.orphan_ms > 40000


def test_budget_blown(analysis):
    assert analysis.incident.blown_budget is True


def test_chains_enumerated(analysis):
    assert len(analysis.chains) > 0
    for c in analysis.chains[:20]:
        assert c.leaf_amplification >= 1
        assert c.chain.depth >= 1


# ---------------------------------------------------------------------------
# 模拟器一致性
# ---------------------------------------------------------------------------
def test_engines_agree_clean(company, resolved):
    """无饱和、确定性时长时，两台引擎逐边放大倍数完全一致。"""
    chain = ["edge-gateway-01", "order-bff-01", "order-svc-01", "inventory-svc-01"]
    edges = [f"{a}->{b}" for a, b in zip(chain, chain[1:])]
    sc = Scenario(
        root="edge-gateway-01",
        faults=[FaultSpec("inventory-svc-01", error_rate=1.0, start_ms=0.0)],
        duration_ms=900000.0, entry_rps=0.2, seed=5,
        deterministic_durations=True,
        services=chain, edges=edges,
        max_root_requests=40, drain=True,
    )
    a = run_stepped(company, resolved, sc).result()
    b = run_des(company, resolved, sc).result()
    assert a.root_requests == b.root_requests == 40
    for eid in edges:
        assert a.edge_stats[eid]["attempts"] == b.edge_stats[eid]["attempts"], eid


def test_converged_check_exact(company):
    chain = ["edge-gateway-01", "order-bff-01", "order-svc-01", "inventory-svc-01"]
    sc = scenario_from_incident(company)
    v = converged_check(company, chain, sc, n_requests=30)
    assert v.ok, v.to_dict()
    assert v.max_rel_err < 1e-6
    # 明确的 1 / 8 / 24
    by = {r.edge_id: r for r in v.rows}
    assert by["order-bff-01->order-svc-01"].analytical == 8
    assert by["order-svc-01->inventory-svc-01"].analytical == 24
    assert by["order-svc-01->inventory-svc-01"].stepped == 24
    assert by["order-svc-01->inventory-svc-01"].des == 24


def test_amplification_bound():
    chain = ["a", "b", "c"]
    t = linear_topo([
        ("a->b", "a", "b", [EdgeConfig("code", retries=1)]),
        ("b->c", "b", "c", [EdgeConfig("code", retries=2)]),
    ])
    r = resolve_all(t)
    bound = amplification_bound(t, r, chain)
    assert bound["a->b"] == 2
    assert bound["b->c"] == 6


def test_simulation_reproduces_incident(company):
    svc = AppService()
    d = svc.reproduce_incident(entry_rps=4.0, duration_ms=60000.0)
    assert d["verdict"]["root_error_rate"] > 0.5
    assert "inventory-svc-01" in d["verdict"]["downstream_saturated"]
    leaf = [c for c in d["chain_stats"] if c["edge_id"].endswith("inventory-svc-01")][0]
    assert leaf["amplification"] > 10
    assert leaf["rejected"] > 100


def test_incident_low_load_before_fault_is_clean():
    """故障注入前（前 5s）不该有拒绝。"""
    svc = AppService()
    sc = svc.incident_scenario(entry_rps=4.0, duration_ms=60000.0)
    res = run_stepped(svc.store().topo, svc.store().resolved, sc).result()
    early = [f for f in res.frames if f["t"] < 5000]
    for f in early:
        assert f["services"]["inventory-svc-01"]["rejected"] == 0
    late = [f for f in res.frames if f["t"] > 40000]
    assert any(f["services"]["inventory-svc-01"]["queue"] > 50 for f in late)


def test_pick_root_covers_full_chain(company):
    """给中间层服务注入故障时，入口应选到最外层网关，而不是它自己或它的直接上游。"""
    from cascade.scenario import build_focus, pick_root_for

    root = pick_root_for(company, "inventory-svc-01")
    assert root == "edge-gateway-01"
    sc = Scenario(root=root, faults=[FaultSpec("inventory-svc-01", slow_factor=2.2)])
    fg = build_focus(company, sc)
    # 必须覆盖事故链三段
    assert "order-bff-01->order-svc-01" in fg.edges
    assert "order-svc-01->inventory-svc-01" in fg.edges
    # 绝不能把故障服务自己当入口
    assert pick_root_for(company, "edge-gateway-01") == "edge-gateway-01"


# ---------------------------------------------------------------------------
# 建议
# ---------------------------------------------------------------------------
def test_proposals_reduce_amplification(company, analysis):
    props = {p.key: p for p in build_proposals(company, analysis)}
    chain = ["edge-gateway-01", "order-bff-01", "order-svc-01", "inventory-svc-01"]
    old = analysis.incident.leaf_amplification  # 24
    # 这三条是**静态**改写重试次数，解析出的上限应下降
    for key in ("retry_inner", "single_layer", "combo"):
        p = props[key]
        r = resolve_all(p.topo)
        bound = amplification_bound(p.topo, r, chain)
        assert bound["order-svc-01->inventory-svc-01"] < old, key
    # retry_budget 是**运行时**封顶（token bucket），静态 attempts 不变，
    # 但实际推演出的流量应大幅下降——见 test_combo_fixes_saturation 与
    # test_retry_budget_caps_runtime 的对照。


def test_combo_fixes_saturation(company):
    svc = AppService()
    d = svc.evaluate_proposal("combo", entry_rps=4.0, duration_ms=60000.0)
    # 改前：叶子被打到容量上限，出现大量拒绝
    assert d["before"]["n_saturated"] >= 1
    assert d["before"]["leaf_amplification"] > 10
    # 改后：放大降到 1，且不再有服务被打满
    assert d["after"]["leaf_amplification"] < 2.0
    assert d["after"]["n_saturated"] == 0


def test_timeout_desc_does_not_make_worse(company):
    """timeout_desc 不能把超时切到下游正常耗时以下（否则正常流量也失败）。"""
    svc = AppService()
    d = svc.evaluate_proposal("timeout_desc", entry_rps=4.0, duration_ms=60000.0)
    # 不应比改前错误率更高
    assert d["after"]["root_error_rate"] <= d["before"]["root_error_rate"] + 1e-6


def test_retry_budget_caps_runtime(company):
    """重试预算是运行时封顶：静态 attempts 不变，但实际流量被压低。"""
    svc = AppService()
    d = svc.evaluate_proposal("retry_budget", entry_rps=4.0, duration_ms=60000.0)
    assert d["after"]["leaf_amplification"] < d["before"]["leaf_amplification"]
    # 1.0 表示重试被预算挡住，叶子几乎只收到首发流量
    assert d["after"]["leaf_amplification"] <= 1.1


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
def test_api_endpoints():
    from cascade.api import create_app

    app = create_app(AppService())
    c = app.test_client()
    for path in ["/api/overview", "/api/topology", "/api/inversions",
                 "/api/amplification", "/api/proposals", "/api/incident"]:
        r = c.get(path)
        assert r.status_code == 200, path
    r = c.get("/api/edge/order-bff-01->order-svc-01")
    assert r.status_code == 200
    assert r.get_json()["resolved"]["attempts"] == 8
    r = c.post("/api/simulate", json={"service": "inventory-svc-01", "slow_factor": 2.2})
    assert r.status_code == 200
    assert "frames" in r.get_json()
    r = c.get("/api/verify/incident")
    assert r.get_json()["converged"]["ok"] is True
