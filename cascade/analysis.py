"""拓扑分析：调用链、超时倒挂、重试放大。

## 三个概念

* **倒挂 (inversion)**：上游给这条边的总时间 < 下游把重试做完需要的时间。
  上游早已超时放弃，下游仍在重试——放大的流量白打，且下游被持续加压。
  判定用最坏情况耗时 ``attempts × timeout + backoff_total``。

* **重试放大 (amplification)**：一条链路上，一个入口请求最坏情况会在各层
  产生多少次请求。若第 i 层一次调用放大 ``a_i = attempts_i`` 倍，且逐层
  串行放大，则到叶子层单个请求被放大 ``Π a_i``。并行扇出时分母不同。

* **危险叠加**：链路上连续多层都重试，且每层最坏耗时超过上游预算，
  放大倍数相乘、倒挂逐层累积。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .model import Topology
from .resolve import ResolvedEdge


# ---------------------------------------------------------------------------
# 调用链枚举
# ---------------------------------------------------------------------------

@dataclass
class Chain:
    nodes: List[str]  # [root, ..., leaf]
    edges: List[str]  # edge ids, len = len(nodes)-1
    #: 每跳的业务调用比例乘积（扇出时整条路径的到达概率）
    path_prob: float

    @property
    def root(self) -> str:
        return self.nodes[0]

    @property
    def leaf(self) -> str:
        return self.nodes[-1]

    @property
    def depth(self) -> int:
        return len(self.nodes) - 1


def enumerate_chains(topo: Topology, max_chains: int = 20000, max_depth: int = 12) -> List[Chain]:
    """从所有入口出发 DFS 枚举到叶子（无出边）的路径，做成 DAG 去重。"""
    chains: List[Chain] = []
    out = {sid: topo.out_edges(sid) for sid in topo.services}

    def dfs(node: str, nodes: List[str], edges: List[str], prob: float, seen: Set[str]):
        if len(chains) >= max_chains:
            return
        outs = [e for e in out[node] if e.callee not in seen]
        if not outs or len(nodes) > max_depth:
            if len(nodes) > 1:
                chains.append(Chain(list(nodes), list(edges), prob))
            return
        for e in outs:
            nodes.append(e.callee)
            edges.append(e.id)
            dfs(e.callee, nodes, edges, prob * max(1e-6, e.call_prob), seen | {e.callee})
            nodes.pop()
            edges.pop()

    for root in topo.roots:
        if root in topo.services:
            dfs(root, [root], [], 1.0, {root})
    return chains


def incident_chain(topo: Topology) -> Optional[Chain]:
    meta = topo.meta.get("incident")
    if not meta:
        return None
    nodes = meta["chain"]
    ids = [f"{a}->{b}" for a, b in zip(nodes, nodes[1:])]
    if not all(eid in topo.edges for eid in ids):
        return None
    prob = 1.0
    for eid in ids:
        prob *= topo.edges[eid].call_prob
    return Chain(nodes=list(nodes), edges=ids, path_prob=prob)


# ---------------------------------------------------------------------------
# 单边与单链指标
# ---------------------------------------------------------------------------

@dataclass
class EdgeMetrics:
    edge_id: str
    caller: str
    callee: str
    timeout_ms: float
    attempts: int
    worst_case_ms: float
    #: 上游（caller）对"一次调用"愿意等的总时间，若 caller 是入口则用 SLO，
    #: 否则用 caller 入边的最坏情况；无法判定时为 None
    upstream_budget_ms: Optional[float]
    inverted: bool
    #: 下游把重试做完需要的最坏耗时
    downstream_ms: float
    #: 上游放弃后，下沉流量仍会打多久
    orphan_ms: float = 0.0
    app_retries: int = 0
    mesh_retries: int = 0
    propagate_deadline: bool = False

    @property
    def amplification(self) -> int:
        return self.attempts


@dataclass
class ChainMetrics:
    chain: Chain
    edge_metrics: List[EdgeMetrics]
    #: 每个请求到达叶子时，最坏在叶子被处理的次数
    leaf_amplification: int
    #: 链路总最坏耗时（串行累加各跳最坏耗时）
    total_worst_ms: float
    #: 入口 SLO / 预算
    budget_ms: Optional[float]
    #: 超时倒挂的边
    inverted_edges: List[str]
    #: 最坏耗时超过入口预算的跳
    blown_budget: bool
    #: 危险叠加层数（连续重试且倒挂的层数）
    danger_layers: int

    @property
    def amplification_layers(self) -> List[Tuple[str, int]]:
        return [(m.edge_id, m.attempts) for m in self.edge_metrics if m.attempts > 1]


def _upstream_budget(topo: Topology, resolved: Dict[str, ResolvedEdge], node: str) -> Optional[float]:
    """node 作为调用方时，一次调用它自己有多少时间可用。"""
    svc = topo.services[node]
    if node in topo.roots and svc.slo_ms is not None:
        return float(svc.slo_ms)
    # 非入口：取它上游边里最宽松的一次尝试超时（调用它的那层愿意等多久）
    ins = topo.in_edges(node)
    if not ins:
        return None
    vals = [resolved[e.id].timeout_ms for e in ins if e.id in resolved]
    return max(vals) if vals else None


def analyze_edge(topo: Topology, resolved: Dict[str, ResolvedEdge], edge_id: str) -> EdgeMetrics:
    re = resolved[edge_id]
    e = topo.edges[edge_id]
    worst = re.worst_case_ms()
    budget = _upstream_budget(topo, resolved, e.caller)
    inverted = budget is not None and worst > budget + 1e-9
    orphan = max(0.0, worst - budget) if budget is not None else 0.0
    return EdgeMetrics(
        edge_id=edge_id,
        caller=e.caller,
        callee=e.callee,
        timeout_ms=re.timeout_ms,
        attempts=re.attempts,
        worst_case_ms=worst,
        upstream_budget_ms=budget,
        inverted=inverted,
        downstream_ms=worst,
        orphan_ms=orphan,
        app_retries=re.app_retries,
        mesh_retries=re.mesh_retries,
        propagate_deadline=re.propagate_deadline,
    )


def analyze_all_edges(topo: Topology, resolved: Dict[str, ResolvedEdge]) -> Dict[str, EdgeMetrics]:
    return {eid: analyze_edge(topo, resolved, eid) for eid in topo.edges}


def analyze_chain(
    topo: Topology,
    resolved: Dict[str, ResolvedEdge],
    em: Dict[str, EdgeMetrics],
    chain: Chain,
) -> ChainMetrics:
    ems = [em[eid] for eid in chain.edges]
    leaf_amp = 1
    total = 0.0
    run = 0
    danger = 0
    for m in ems:
        leaf_amp *= m.attempts
        total += m.worst_case_ms  # 沿一条路径调用天然串行
        if m.attempts > 1:
            run += 1
            danger = max(danger, run)
        else:
            run = 0
    root_svc = topo.services[chain.root]
    budget = root_svc.slo_ms
    blown = budget is not None and total > budget
    return ChainMetrics(
        chain=chain,
        edge_metrics=ems,
        leaf_amplification=leaf_amp,
        total_worst_ms=total,
        budget_ms=budget,
        inverted_edges=[m.edge_id for m in ems if m.inverted],
        blown_budget=blown,
        danger_layers=danger,
    )


@dataclass
class AnalysisResult:
    edges: Dict[str, EdgeMetrics]
    chains: List[ChainMetrics]
    incident: Optional[ChainMetrics]
    totals: dict = field(default_factory=dict)


def analyze(topo: Topology, resolved: Optional[Dict[str, ResolvedEdge]] = None,
            max_chains: int = 20000) -> AnalysisResult:
    from .resolve import resolve_all

    resolved = resolved or resolve_all(topo)
    em = analyze_all_edges(topo, resolved)
    chains = enumerate_chains(topo, max_chains=max_chains)
    cms = [analyze_chain(topo, resolved, em, c) for c in chains]
    ic = incident_chain(topo)
    icm = analyze_chain(topo, resolved, em, ic) if ic else None

    by_amp = sorted(cms, key=lambda x: x.leaf_amplification, reverse=True)
    totals = {
        "n_services": len(topo.services),
        "n_edges": len(topo.edges),
        "n_chains": len(cms),
        "n_inverted_edges": sum(1 for m in em.values() if m.inverted),
        "n_retrying_edges": sum(1 for m in em.values() if m.attempts > 1),
        "max_leaf_amplification": by_amp[0].leaf_amplification if by_amp else 1,
        "worst_chain": by_amp[0].chain.nodes if by_amp else [],
        "n_chains_blown_budget": sum(1 for c in cms if c.blown_budget),
        "incident_amplification": icm.leaf_amplification if icm else None,
    }
    return AnalysisResult(edges=em, chains=cms, incident=icm, totals=totals)
