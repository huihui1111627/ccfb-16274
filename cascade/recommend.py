"""修改建议与"改完再推演"。

每条建议都是一个**可执行变换**：作用于拓扑副本，产生新的三来源配置，
然后可以重新解析、重新分析、重新推演，用数据证明有效。

四类建议：

* ``retry_inner``    —— 只在最内层重试：外层重试清零，保留叶子层一次。
* ``timeout_desc``   —— 超时沿链路逐层递减：每跳超时 ≈ 上游剩余预算 / 常数。
* ``retry_budget``   —— 加重试预算：每边重试比例封顶（默认 10%）。
* ``single_layer``   —— 去掉网格层的重试，避免与应用层相乘。

以及组合方案 ``combo``（逐层递减 + 只在内层重试 + 预算）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import EdgeConfig, Topology
from .resolve import ResolvedEdge, resolve_all


@dataclass
class Change:
    kind: str
    edge_id: str
    field: str
    old: object
    new: object
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "edge_id": self.edge_id,
            "field": self.field,
            "old": self.old,
            "new": self.new,
            "note": self.note,
        }


@dataclass
class Proposal:
    key: str
    title: str
    rationale: str
    changes: List[Change] = field(default_factory=list)
    #: 被建议修改的边
    target_edges: List[str] = field(default_factory=list)
    topo: Optional[Topology] = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "rationale": self.rationale,
            "changes": [c.to_dict() for c in self.changes],
            "target_edges": self.target_edges,
        }


def _proposed_cfg(values: Dict[str, object]) -> EdgeConfig:
    """把解析后的最终值包成一条最高优先级的"建议方案"规则。"""
    return EdgeConfig(
        source="proposed",
        priority=1000,
        label="建议方案",
        timeout_ms=values.get("timeout_ms"),  # type: ignore[arg-type]
        retries=values.get("retries"),  # type: ignore[arg-type]
        backoff_base_ms=values.get("backoff_base_ms"),  # type: ignore[arg-type]
        backoff_mult=values.get("backoff_mult"),  # type: ignore[arg-type]
        backoff_max_ms=values.get("backoff_max_ms"),  # type: ignore[arg-type]
        jitter=values.get("jitter"),  # type: ignore[arg-type]
        retry_budget=values.get("retry_budget"),  # type: ignore[arg-type]
        propagate_deadline=values.get("propagate_deadline"),  # type: ignore[arg-type]
    )


def _clone_with(topo: Topology, edits: Dict[str, Dict[str, object]]) -> Topology:
    """复制拓扑并把 edits 作为 proposed 规则叠加到指定边。"""
    new = topo.clone()
    for eid, vals in edits.items():
        edge = new.edges[eid]
        edge.configs = [c for c in edge.configs if c.source != "proposed"]
        edge.add_config(_proposed_cfg(vals))
    return new


def _resolved_values(re: ResolvedEdge) -> Dict[str, object]:
    return dict(re.values)


# ---------------------------------------------------------------------------
# 各建议
# ---------------------------------------------------------------------------

def chains_of_interest(topo: Topology, analysis, limit: int = 12) -> List[List[str]]:
    """挑出值得关注的链路：事故链 + 放大最大 + 有倒挂的。"""
    out: List[List[str]] = []
    seen = set()

    def add(c):
        key = tuple(c.chain.nodes)
        if key not in seen:
            seen.add(key)
            out.append(c.chain.nodes)

    if analysis.incident:
        add(analysis.incident)
    ranked = sorted(
        analysis.chains,
        key=lambda c: (len(c.inverted_edges), c.leaf_amplification),
        reverse=True,
    )
    for c in ranked:
        if len(out) >= limit:
            break
        if c.inverted_edges or c.leaf_amplification > 2:
            add(c)
    return out


def _leaf_edges(chains: List[List[str]], topo: Topology) -> set:
    """在关注链路集合中，作为"最后一跳"出现过的边。

    同一条边可能在一条链里是叶子、在另一条里是中转。只要它在任一条被研究的
    链里处于最内层，就允许它保留一次重试；其余边一律不重试。
    这样语义确定，不会因为链的遍历顺序不同而得到不同结果。
    """
    out = set()
    for nodes in chains:
        edges = [f"{a}->{b}" for a, b in zip(nodes, nodes[1:])]
        if edges and edges[-1] in topo.edges:
            out.add(edges[-1])
    return out


def propose_retry_inner_only(topo: Topology, analysis, chains: List[List[str]]) -> Proposal:
    resolved = resolve_all(topo)
    leaf = _leaf_edges(chains, topo)
    all_edges = set()
    for nodes in chains:
        all_edges.update(f"{a}->{b}" for a, b in zip(nodes, nodes[1:]))
    edits: Dict[str, Dict[str, object]] = {}
    changes: List[Change] = []
    for eid in sorted(all_edges):
        if eid not in topo.edges:
            continue
        re = resolved[eid]
        desired = 1 if eid in leaf else 0
        if re.effective_retries != desired:
            vals = _resolved_values(re)
            vals["retries"] = desired
            edits[eid] = vals
            changes.append(
                Change(
                    kind="retry_inner",
                    edge_id=eid,
                    field="retries",
                    old=re.effective_retries,
                    new=desired,
                    note="最内层保留一次重试" if desired else "外层不再重试，交给最内层",
                )
            )
    return Proposal(
        key="retry_inner",
        title="只在最内层重试",
        rationale=(
            "外层重试与内层重试会相乘。把重试收敛到每一条链的最内层一次，"
            "上层只做超时控制，放大倍数从 Π(1+r_i) 降到 1+r_leaf。"
        ),
        changes=changes,
        target_edges=sorted(edits),
        topo=_clone_with(topo, edits) if edits else topo.clone(),
    )


def _downstream_normal_ms(topo: Topology, nodes: List[str], i: int) -> float:
    """第 i 跳的**下游子树在正常情况下的耗时**（沿这条链累加 service_ms）。

    这是该跳超时的物理下限：超时设得比它还小，即使下游完全健康也必然失败。
    """
    return sum(topo.services[n].service_ms for n in nodes[i + 1:])


def propose_timeout_descending(topo: Topology, analysis, chains: List[List[str]],
                               safety: float = 1.6) -> Proposal:
    """逐层递减，但保证每一跳的超时都能容纳其下游的正常耗时。

    正确做法：第 i 跳超时 = max(下游正常耗时 × safety, 按剩余预算分摊)。
    同一条边可能出现在多条链里（下游不同），取**所有出现中最宽松的下限**，
    否则会被某条"下游很轻"的链把超时压到不合理的小值。
    """
    resolved = resolve_all(topo)
    # 先按边汇总：对每条边，在所有包含它的链里算候选下限，保留最大值
    per_edge: Dict[str, List[float]] = {}
    per_edge_ctx: Dict[str, str] = {}
    for nodes in chains:
        edges = [f"{a}->{b}" for a, b in zip(nodes, nodes[1:])]
        root = topo.services[nodes[0]]
        budget = root.slo_ms or 3000.0
        for i, eid in enumerate(edges):
            if eid not in topo.edges:
                continue
            re = resolved[eid]
            floor = _downstream_normal_ms(topo, nodes, i) * safety
            remaining = budget * (0.65 ** i)
            share = remaining / max(1, re.attempts)
            cand = max(floor, share, 2.0 * topo.services[nodes[i + 1]].service_ms)
            if eid not in per_edge or cand > max(per_edge[eid]):
                per_edge_ctx[eid] = (
                    f"下游正常耗时 {floor / safety:.0f}ms × {safety:g} 安全系数"
                )
            per_edge.setdefault(eid, []).append(cand)

    edits: Dict[str, Dict[str, object]] = {}
    changes: List[Change] = []
    target = []
    for eid, cands in per_edge.items():
        re = resolved[eid]
        new_timeout = round(max(cands), -1)
        if abs(new_timeout - re.timeout_ms) > 1.0 or not re.propagate_deadline:
            vals = _resolved_values(re)
            vals["timeout_ms"] = new_timeout
            vals["propagate_deadline"] = True
            edits[eid] = vals
            changes.append(
                Change(
                    kind="timeout_desc",
                    edge_id=eid,
                    field="timeout_ms",
                    old=re.timeout_ms,
                    new=new_timeout,
                    note=per_edge_ctx.get(eid, "") + "，开启超时传递",
                )
            )
            target.append(eid)
    return Proposal(
        key="timeout_desc",
        title="超时沿链路递减（不切进下游正常耗时）",
        rationale=(
            "每一跳的超时都 ≥ 其下游正常耗时，正常流量不受影响；"
            "打开 propagate_deadline 后上游放弃即通知下游中止，"
            "截断『网关已超时、底下还在重试』的空转窗口。"
            "注意：只要重试次数不动，倒挂的最坏耗时仍然超预算——"
            "这条建议负责止住空转，放大倍数要靠重试侧的改动。"
        ),
        changes=changes,
        target_edges=sorted(set(target)),
        topo=_clone_with(topo, edits) if edits else topo.clone(),
    )


def propose_retry_budget(topo: Topology, analysis, chains: List[List[str]],
                         budget: float = 0.1) -> Proposal:
    resolved = resolve_all(topo)
    edits: Dict[str, Dict[str, object]] = {}
    changes: List[Change] = []
    seen: set = set()
    for nodes in chains:
        edges = [f"{a}->{b}" for a, b in zip(nodes, nodes[1:])]
        for eid in edges:
            if eid not in topo.edges or eid in seen:
                continue
            seen.add(eid)
            re = resolved[eid]
            if re.attempts <= 1:
                continue
            vals = _resolved_values(re)
            vals["retry_budget"] = budget
            edits[eid] = vals
            changes.append(
                Change(
                    kind="retry_budget",
                    edge_id=eid,
                    field="retry_budget",
                    old=re.retry_budget,
                    new=budget,
                    note=f"重试不得超过首发的 {budget:.0%}",
                )
            )
    return Proposal(
        key="retry_budget",
        title="加装重试预算（token bucket）",
        rationale=(
            "无论下游多慢，重试流量都被限制在首发流量的固定比例内，"
            "避免故障时重试风暴把下游彻底压垮。"
        ),
        changes=changes,
        target_edges=sorted(edits),
        topo=_clone_with(topo, edits) if edits else topo.clone(),
    )


def propose_single_layer(topo: Topology, analysis, chains: List[List[str]]) -> Proposal:
    resolved = resolve_all(topo)
    edits: Dict[str, Dict[str, object]] = {}
    changes: List[Change] = []
    seen: set = set()
    for nodes in chains:
        edges = [f"{a}->{b}" for a, b in zip(nodes, nodes[1:])]
        for eid in edges:
            if eid not in topo.edges or eid in seen:
                continue
            seen.add(eid)
            re = resolved[eid]
            if re.app_retries > 0 and re.mesh_retries > 0:
                vals = _resolved_values(re)
                # 合成单层次数：取两者较大者，不再相乘
                vals["retries"] = max(re.app_retries, re.mesh_retries)
                edits[eid] = vals
                changes.append(
                    Change(
                        kind="single_layer",
                        edge_id=eid,
                        field="retries",
                        old=re.attempts - 1,
                        new=int(vals["retries"]),
                        note=(
                            f"应用层 {re.app_retries} 次 × 网格层 {re.mesh_retries} 次，"
                            f"改为单层 {max(re.app_retries, re.mesh_retries)} 次"
                        ),
                    )
                )
    return Proposal(
        key="single_layer",
        title="网格层与应用层不要同时重试",
        rationale=(
            "两层重试是相乘关系。同一跳只允许一层重试，另一层只负责超时。"
        ),
        changes=changes,
        target_edges=sorted(edits),
        topo=_clone_with(topo, edits) if edits else topo.clone(),
    )


def propose_combo(topo: Topology, analysis, chains: List[List[str]]) -> Proposal:
    """逐层递减 + 只在内层重试 + 预算，三者叠加。

    按边汇总，避免同一条边在不同链里取到互相矛盾的值：
    超时取各链候选的最大值（保证不被"轻下游"的链压垮），
    重试按"是否在任一条链里是最内层"决定。
    """
    resolved = resolve_all(topo)
    leaf = _leaf_edges(chains, topo)

    # 每条边：在所有链里能给出最宽松上限的候选超时
    timeouts: Dict[str, float] = {}
    for nodes in chains:
        edges = [f"{a}->{b}" for a, b in zip(nodes, nodes[1:])]
        root = topo.services[nodes[0]]
        budget = root.slo_ms or 3000.0
        for i, eid in enumerate(edges):
            if eid not in topo.edges:
                continue
            callee = topo.services[nodes[i + 1]]
            floor = _downstream_normal_ms(topo, nodes, i) * 1.5
            remaining = budget * (0.65 ** i)
            attempts = 2 if eid in leaf else 1
            cand = max(floor, remaining / attempts, 2.0 * callee.service_ms)
            timeouts[eid] = max(timeouts.get(eid, 0.0), cand)

    edits: Dict[str, Dict[str, object]] = {}
    changes: List[Change] = []
    for eid in sorted(timeouts):
        re = resolved[eid]
        vals = _resolved_values(re)
        new_timeout = round(timeouts[eid], -1)
        if abs(new_timeout - re.timeout_ms) > 1.0:
            changes.append(Change("timeout_desc", eid, "timeout_ms", re.timeout_ms, new_timeout, "逐层递减并传递预算"))
        vals["timeout_ms"] = new_timeout
        vals["propagate_deadline"] = True
        desired_r = 1 if eid in leaf else 0
        if re.effective_retries != desired_r:
            changes.append(Change("retry_inner", eid, "retries", re.effective_retries, desired_r, "只在内层重试"))
        vals["retries"] = desired_r
        if desired_r > 0 and re.retry_budget != 0.1:
            vals["retry_budget"] = 0.1
            changes.append(Change("retry_budget", eid, "retry_budget", re.retry_budget, 0.1, "10% 预算"))
        edits[eid] = vals
    return Proposal(
        key="combo",
        title="组合方案：逐层递减 + 只在内层重试 + 预算",
        rationale=(
            "三条规则一起用：放大倍数降到 2 左右、每一跳都有足够预算、"
            "且异常时重试流量有硬上限。"
        ),
        changes=changes,
        target_edges=sorted(edits),
        topo=_clone_with(topo, edits) if edits else topo.clone(),
    )


PROPOSAL_BUILDERS = {
    "retry_inner": propose_retry_inner_only,
    "timeout_desc": propose_timeout_descending,
    "retry_budget": propose_retry_budget,
    "single_layer": propose_single_layer,
    "combo": propose_combo,
}


def build_proposals(topo: Topology, analysis, limit_chains: int = 12) -> List[Proposal]:
    chains = chains_of_interest(topo, analysis, limit_chains)
    props = []
    for key in ("retry_inner", "timeout_desc", "retry_budget", "single_layer", "combo"):
        props.append(PROPOSAL_BUILDERS[key](topo, analysis, chains))
    return props


def summary_of(prop: Proposal, topo: Topology, analysis) -> dict:
    """对一个建议方案做静态分析摘要（不推演）。"""
    from .analysis import analyze

    new_resolved = resolve_all(prop.topo)
    new_analysis = analyze(prop.topo, new_resolved)
    old_amp = analysis.incident.leaf_amplification if analysis.incident else None
    new_inc = new_analysis.incident
    return {
        "key": prop.key,
        "title": prop.title,
        "rationale": prop.rationale,
        "n_changes": len(prop.changes),
        "target_edges": prop.target_edges,
        "changes": [c.to_dict() for c in prop.changes],
        "before": {
            "incident_amplification": old_amp,
            "n_inverted_edges": analysis.totals.get("n_inverted_edges"),
            "max_leaf_amplification": analysis.totals.get("max_leaf_amplification"),
        },
        "after": {
            "incident_amplification": new_inc.leaf_amplification if new_inc else None,
            "n_inverted_edges": new_analysis.totals.get("n_inverted_edges"),
            "max_leaf_amplification": new_analysis.totals.get("max_leaf_amplification"),
        },
    }
