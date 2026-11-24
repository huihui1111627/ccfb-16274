"""对外服务层：把拓扑、解析、分析、推演、建议、核对串成 API。

Web 层只依赖这里的函数，保持路由薄。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from .analysis import AnalysisResult, analyze
from .generator import GeneratorSettings, build_company
from .model import SOURCE_LABEL, SOURCE_LAYER, Topology
from .recommend import Proposal, build_proposals, summary_of
from .resolve import ResolvedEdge, resolve_all
from .scenario import (
    FaultSpec,
    Scenario,
    SimResult,
    build_focus,
    pick_root_for,
    scenario_from_incident,
)
from .sim_des import run_des
from .sim_stepped import run_stepped
from .verify import amplification_bound, compare_amplification, converged_check


@dataclass
class Store:
    topo: Topology
    resolved: Dict[str, ResolvedEdge]
    analysis: AnalysisResult


class Service:
    def __init__(self, settings: Optional[GeneratorSettings] = None):
        self.settings = settings or GeneratorSettings()
        self._lock = threading.Lock()
        self._store: Optional[Store] = None

    # ------------------------------------------------------------------
    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                topo = build_company(self.settings)
                resolved = resolve_all(topo)
                analysis = analyze(topo, resolved)
                self._store = Store(topo, resolved, analysis)
            return self._store

    def reload(self, seed: Optional[int] = None, n_services: Optional[int] = None) -> None:
        with self._lock:
            if seed is not None:
                self.settings.seed = seed
            if n_services is not None:
                self.settings.n_services = n_services
            self._store = None

    # ------------------------------------------------------------------
    # 概览
    # ------------------------------------------------------------------
    def overview(self) -> dict:
        st = self.store()
        topo = st.topo
        meta = topo.meta.get("incident", {})
        # 入口列表摘要
        roots = []
        for r in topo.roots:
            svc = topo.services[r]
            roots.append({"id": r, "name": svc.name, "domain": svc.domain})
        return {
            "company": topo.meta.get("company"),
            "seed": topo.seed,
            "n_services": len(topo.services),
            "n_edges": len(topo.edges),
            "n_roots": len(topo.roots),
            "roots": roots[:40],
            "domains": _domain_summary(topo),
            "totals": st.analysis.totals,
            "incident": {
                "root": meta.get("root"),
                "chain": meta.get("chain"),
                "slo_ms": meta.get("slo_ms"),
                "slow_service": meta.get("slow_service"),
                "story": meta.get("story"),
                "amplification": (
                    st.analysis.incident.leaf_amplification
                    if st.analysis.incident else None
                ),
            },
        }

    # ------------------------------------------------------------------
    # 拓扑（图）
    # ------------------------------------------------------------------
    def topology(self, domain: Optional[str] = None, max_nodes: int = 400,
                 mode: str = "relevant") -> dict:
        st = self.store()
        topo = st.topo
        incident_nodes = set(topo.meta.get("incident", {}).get("chain", []))

        keep = set()
        if mode == "incident":
            # 只看事故链 + 直接邻居
            keep |= incident_nodes
            for e in topo.edges.values():
                if e.caller in incident_nodes:
                    keep.add(e.callee)
                if e.callee in incident_nodes:
                    keep.add(e.caller)
        elif mode == "inverted":
            # 只保留倒挂边及其端点——这是"要修的东西"的最小集合
            keep |= incident_nodes
            for eid, em in st.analysis.edges.items():
                if em.inverted:
                    keep.add(em.caller)
                    keep.add(em.callee)
        elif mode == "relevant":
            # 关键子图：事故链 + 倒挂边 + 重试边端点
            keep |= incident_nodes
            for eid, em in st.analysis.edges.items():
                if em.inverted or em.attempts > 1:
                    keep.add(em.caller)
                    keep.add(em.callee)
        if domain:
            keep = {s for s in keep if topo.services[s].domain == domain or s in incident_nodes}

        # 全部服务
        all_ids = set(topo.services)
        if mode == "all":
            keep = all_ids
            if domain:
                keep = {s for s in keep if topo.services[s].domain == domain or s in incident_nodes}

        if len(keep) > max_nodes:
            # 优先保留：事故链 > 倒挂端点 > 重试端点 > 其余
            prio = {}
            for s in keep:
                p = 0
                if s in incident_nodes:
                    p = 3
                elif any(st.analysis.edges[e].inverted for e in [x.id for x in topo.in_edges(s) + topo.out_edges(s)]):
                    p = 2
                elif any(st.analysis.edges[e].attempts > 1 for e in [x.id for x in topo.in_edges(s) + topo.out_edges(s)]):
                    p = 1
                prio[s] = (p, -topo.services[s].tier)
            keep = set(sorted(keep, key=lambda s: prio[s], reverse=True)[:max_nodes])

        nodes = []
        for sid in sorted(keep):
            svc = topo.services[sid]
            nodes.append(
                {
                    "id": sid,
                    "name": svc.name,
                    "tier": svc.tier,
                    "domain": svc.domain,
                    "workers": svc.workers,
                    "service_ms": svc.service_ms,
                    "slo_ms": svc.slo_ms,
                    "root": sid in topo.roots,
                    "incident": sid in incident_nodes,
                }
            )
        edges = []
        for eid, e in topo.edges.items():
            if e.caller not in keep or e.callee not in keep:
                continue
            re = st.resolved[eid]
            em = st.analysis.edges[eid]
            edges.append(
                {
                    "id": eid,
                    "source": e.caller,
                    "target": e.callee,
                    "timeout_ms": re.timeout_ms,
                    "attempts": re.attempts,
                    "worst_case_ms": em.worst_case_ms,
                    "inverted": em.inverted,
                    "orphan_ms": em.orphan_ms,
                    "propagate_deadline": re.propagate_deadline,
                    "retry_budget": re.retry_budget,
                    "incident": (
                        e.caller in incident_nodes and e.callee in incident_nodes
                    ),
                    "call_prob": e.call_prob,
                }
            )
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": len(keep) < len(topo.services),
            "shown": len(nodes),
            "total": len(topo.services),
            "mode": mode,
        }

    # ------------------------------------------------------------------
    # 单边配置解析明细
    # ------------------------------------------------------------------
    def edge_detail(self, edge_id: str) -> dict:
        st = self.store()
        if edge_id not in st.topo.edges:
            raise KeyError(edge_id)
        e = st.topo.edges[edge_id]
        re = st.resolved[edge_id]
        em = st.analysis.edges[edge_id]
        return {
            "edge": {
                "id": edge_id,
                "caller": e.caller,
                "callee": e.callee,
                "call_prob": e.call_prob,
            },
            "sources": [
                {
                    "source": c.source,
                    "source_label": SOURCE_LABEL.get(c.source, c.source),
                    "layer": SOURCE_LAYER.get(c.source, ""),
                    "priority": c.priority,
                    "label": c.label,
                    "fields": c.provided(),
                    "describe": c.describe(),
                    "is_winner": False,
                }
                for c in e.configs
            ],
            "resolved": {
                "timeout_ms": re.timeout_ms,
                "attempts": re.attempts,
                "effective_retries": re.effective_retries,
                "app_retries": re.app_retries,
                "mesh_retries": re.mesh_retries,
                "retry_budget": re.retry_budget,
                "propagate_deadline": re.propagate_deadline,
                "worst_case_ms": em.worst_case_ms,
                "values": re.values,
            },
            "origins": {
                f: {
                    "value": re.origins[f].value,
                    "source": re.origins[f].source,
                    "label": re.origins[f].display,
                }
                for f in re.origins
            },
            "metrics": {
                "inverted": em.inverted,
                "upstream_budget_ms": em.upstream_budget_ms,
                "orphan_ms": em.orphan_ms,
                "downstream_ms": em.downstream_ms,
                "amplification": em.amplification,
            },
            "explain": {
                "timeout_ms": re.explain("timeout_ms"),
                "retries": re.explain("retries"),
            },
        }

    # ------------------------------------------------------------------
    # 倒挂与放大
    # ------------------------------------------------------------------
    def inversions(self, limit: int = 200) -> dict:
        st = self.store()
        rows = []
        for eid, em in st.analysis.edges.items():
            if em.inverted:
                rows.append(
                    {
                        "edge_id": eid,
                        "caller": em.caller,
                        "callee": em.callee,
                        "timeout_ms": em.timeout_ms,
                        "attempts": em.attempts,
                        "downstream_ms": em.downstream_ms,
                        "upstream_budget_ms": em.upstream_budget_ms,
                        "orphan_ms": em.orphan_ms,
                        "severity": (
                            "high"
                            if em.upstream_budget_ms and em.downstream_ms > 3 * em.upstream_budget_ms
                            else "medium"
                        ),
                    }
                )
        rows.sort(key=lambda r: r["orphan_ms"], reverse=True)
        return {
            "count": len(rows),
            "total_edges": len(st.analysis.edges),
            "rows": rows[:limit],
        }

    def amplification(self, limit: int = 40) -> dict:
        st = self.store()
        chains = sorted(
            st.analysis.chains, key=lambda c: c.leaf_amplification, reverse=True
        )
        rows = []
        for c in chains[:limit]:
            layers = [
                {
                    "edge_id": m.edge_id,
                    "attempts": m.attempts,
                    "app_retries": m.app_retries,
                    "mesh_retries": m.mesh_retries,
                    "inverted": m.inverted,
                    "propagate_deadline": m.propagate_deadline,
                }
                for m in c.edge_metrics
            ]
            rows.append(
                {
                    "nodes": c.chain.nodes,
                    "path_prob": c.chain.path_prob,
                    "leaf_amplification": c.leaf_amplification,
                    "total_worst_ms": c.total_worst_ms,
                    "budget_ms": c.budget_ms,
                    "blown_budget": c.blown_budget,
                    "danger_layers": c.danger_layers,
                    "inverted_edges": c.inverted_edges,
                    "layers": layers,
                    "is_incident": (
                        st.topo.meta.get("incident", {}).get("chain") == c.chain.nodes
                    ),
                }
            )
        inc = st.analysis.incident
        return {
            "rows": rows,
            "incident": {
                "nodes": inc.chain.nodes if inc else None,
                "leaf_amplification": inc.leaf_amplification if inc else None,
                "layers": [
                    {"edge_id": m.edge_id, "attempts": m.attempts}
                    for m in (inc.edge_metrics if inc else [])
                ],
            },
            "totals": st.analysis.totals,
        }

    # ------------------------------------------------------------------
    # 推演
    # ------------------------------------------------------------------
    def scenario_for(self, service: str, slow_factor: float = 1.0,
                     error_rate: float = 0.0, start_ms: float = 5000.0,
                     duration_ms: float = 60000.0, entry_rps: float = 30.0,
                     root: Optional[str] = None, dt_ms: float = 25.0,
                     seed: int = 7) -> Scenario:
        st = self.store()
        root = root or pick_root_for(st.topo, service)
        faults = []
        if slow_factor != 1.0 or error_rate > 0:
            faults = [
                FaultSpec(
                    service=service,
                    slow_factor=slow_factor,
                    error_rate=error_rate,
                    start_ms=start_ms,
                )
            ]
        return Scenario(
            root=root,
            faults=faults,
            duration_ms=duration_ms,
            dt_ms=dt_ms,
            entry_rps=entry_rps,
            seed=seed,
        )

    def simulate(self, scenario: Scenario, proposal_key: Optional[str] = None,
                 engine: str = "stepped") -> dict:
        st = self.store()
        topo = st.topo
        resolved = st.resolved
        if proposal_key:
            prop = self._proposal(proposal_key)
            topo = prop.topo
            resolved = resolve_all(topo)
        focus = build_focus(topo, scenario)
        if engine == "des":
            res = run_des(topo, resolved, scenario).result()
        else:
            res = run_stepped(topo, resolved, scenario).result()
        data = res.to_dict(include_frames=(engine == "stepped"))
        data["focus"] = focus.to_dict()
        data["scenario"] = {
            "root": scenario.root,
            "duration_ms": scenario.duration_ms,
            "dt_ms": scenario.dt_ms,
            "entry_rps": scenario.entry_rps,
            "faults": [
                {"service": f.service, "slow_factor": f.slow_factor,
                 "error_rate": f.error_rate, "start_ms": f.start_ms}
                for f in scenario.faults
            ],
        }
        data["saturation"] = self._saturation_summary(res)
        return data

    def _saturation_summary(self, res: SimResult) -> dict:
        saturated = []
        for sid, stt in res.service_stats.items():
            if stt.get("node_saturated"):
                saturated.append(
                    {
                        "service": sid,
                        "max_queue": stt["max_queue"],
                        "peak_util": stt["peak_util"],
                        "rejected": stt.get("rejected", 0),
                        "capacity_rps": stt.get("capacity_rps"),
                    }
                )
        saturated.sort(key=lambda x: x["rejected"], reverse=True)
        return {
            "saturated_services": saturated,
            "n_saturated": len(saturated),
            "root_errors": res.root_errors,
            "root_error_rate": (
                res.root_errors / res.root_requests if res.root_requests else 0.0
            ),
        }

    # ------------------------------------------------------------------
    # 事故
    # ------------------------------------------------------------------
    def incident_scenario(self, **overrides) -> Scenario:
        st = self.store()
        return scenario_from_incident(st.topo, **overrides)

    def reproduce_incident(self, entry_rps: float = 30.0,
                           duration_ms: float = 60000.0) -> dict:
        st = self.store()
        sc = self.incident_scenario(entry_rps=entry_rps, duration_ms=duration_ms)
        data = self.simulate(sc, engine="stepped")
        chain = st.topo.meta["incident"]["chain"]
        edges = [f"{a}->{b}" for a, b in zip(chain, chain[1:])]
        chain_stats = []
        for eid in edges:
            est = data["edge_stats"].get(eid, {})
            chain_stats.append(
                {
                    "edge_id": eid,
                    "attempts": est.get("attempts", 0),
                    "amplification": est.get("amplification", 0.0),
                    "timeouts": est.get("timeouts", 0),
                    "orphaned": est.get("orphaned", 0),
                    "rejected": est.get("rejected", 0),
                }
            )
        return {
            "story": st.topo.meta.get("incident", {}).get("story"),
            "chain": chain,
            "slo_ms": st.topo.meta.get("incident", {}).get("slo_ms"),
            "simulation": data,
            "chain_stats": chain_stats,
            "verdict": {
                "amplification_observed": chain_stats[-1]["amplification"]
                if chain_stats
                else None,
                "root_error_rate": data["saturation"]["root_error_rate"],
                "downstream_saturated": [
                    s["service"] for s in data["saturation"]["saturated_services"]
                ],
            },
        }

    # ------------------------------------------------------------------
    # 建议
    # ------------------------------------------------------------------
    def _proposal(self, key: str) -> Proposal:
        for p in self.proposals():
            if p.key == key:
                return p
        raise KeyError(key)

    def proposals(self) -> List[Proposal]:
        st = self.store()
        if not hasattr(self, "_props"):
            self._props = build_proposals(st.topo, st.analysis)
        return self._props

    def proposals_summary(self) -> dict:
        st = self.store()
        out = []
        for p in self.proposals():
            out.append(summary_of(p, st.topo, st.analysis))
        return {"proposals": out}

    def evaluate_proposal(self, key: str, fault_service: Optional[str] = None,
                          entry_rps: float = 4.0,
                          duration_ms: float = 60000.0) -> dict:
        st = self.store()
        prop = self._proposal(key)
        # 用事故场景（或指定故障）推演
        if fault_service:
            sc = self.scenario_for(
                fault_service,
                slow_factor=260.0,
                start_ms=5000.0,
                entry_rps=entry_rps,
                duration_ms=duration_ms,
            )
        else:
            sc = self.incident_scenario(entry_rps=entry_rps, duration_ms=duration_ms)

        before = self.simulate(sc, engine="stepped")
        after_topo = prop.topo
        after_resolved = resolve_all(after_topo)
        after = run_stepped(after_topo, after_resolved, sc).result().to_dict(
            include_frames=False
        )
        after_sat = self._saturation_summary(
            run_stepped(after_topo, after_resolved, sc).result()
        )
        chain = st.topo.meta["incident"]["chain"]
        edges = [f"{a}->{b}" for a, b in zip(chain, chain[1:])]

        def chain_amp(data):
            return {
                eid: data["edge_stats"].get(eid, {}).get("amplification", 0.0)
                for eid in edges
            }

        return {
            "key": key,
            "title": prop.title,
            "rationale": prop.rationale,
            "n_changes": len(prop.changes),
            "changes": [c.to_dict() for c in prop.changes][:200],
            "before": {
                "chain_amplification": chain_amp(before),
                "root_error_rate": before["saturation"]["root_error_rate"],
                "n_saturated": before["saturation"]["n_saturated"],
                "leaf_amplification": before["edge_stats"].get(edges[-1], {}).get(
                    "amplification", 0.0
                ) if edges else 0.0,
            },
            "after": {
                "chain_amplification": chain_amp(after),
                "root_error_rate": after_sat["root_error_rate"],
                "n_saturated": after_sat["n_saturated"],
                "leaf_amplification": after["edge_stats"].get(edges[-1], {}).get(
                    "amplification", 0.0
                ) if edges else 0.0,
            },
            "chain": chain,
        }

    # ------------------------------------------------------------------
    # 核对
    # ------------------------------------------------------------------
    def verify_incident(self, n_requests: int = 40) -> dict:
        st = self.store()
        chain = st.topo.meta["incident"]["chain"]
        sc = self.incident_scenario()
        v = converged_check(st.topo, chain, sc, n_requests=n_requests)
        bound = amplification_bound(st.topo, st.resolved, chain)
        return {
            "converged": v.to_dict(),
            "bound": bound,
            "chain": chain,
        }

    def verify_scenario(self, service: str, slow_factor: float = 260.0,
                        error_rate: float = 0.0, entry_rps: float = 30.0,
                        duration_ms: float = 60000.0) -> dict:
        """饱和工况下的三方核对（容差 15%）。"""
        st = self.store()
        sc = self.scenario_for(
            service,
            slow_factor=slow_factor,
            error_rate=error_rate,
            start_ms=0.0,
            entry_rps=entry_rps,
            duration_ms=duration_ms,
        )
        focus = build_focus(st.topo, sc)
        # 解析上限：每条边相对入口的尝试上限不好直接给，退化为 None（只比对两台模拟器）
        v = compare_amplification(
            st.topo, st.resolved, st.analysis, sc, mode="saturated", tol=0.15
        )
        return {"verdict": v.to_dict(), "focus": focus.to_dict()}


def _domain_summary(topo: Topology) -> List[dict]:
    counts: Dict[str, int] = {}
    for svc in topo.services.values():
        counts[svc.domain] = counts.get(svc.domain, 0) + 1
    return [{"domain": d, "count": c} for d, c in sorted(counts.items(), key=lambda x: -x[1])]
